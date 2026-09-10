"""Repeatable integration experiments. Captures only the explicitly named device.

All preview tests here are headless: they test transport/device ownership, NOT
visible preview latency or audio synchronisation perceived by a person.
"""
import argparse
import dataclasses
import json
from pathlib import Path
import shutil
import socket
import subprocess as sp
import time
import uuid
import threading
from unittest.mock import patch

from recording_lab import Config, CREATE_FLAGS, ENCODERS, choose_encoder, device_token, inspect, record, run, save_json


def shared_device(c, root):
    folder = Path(root) / ("sharing-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    log_path = folder / "mpv.log"
    args = [c.mpv, "--no-config", "--load-scripts=no", "--vo=null", "--ao=null", "--untimed",
            "--no-terminal", "--length=25", "--log-file=" + str(log_path.resolve()),
            "--cache=no", "--demuxer-lavf-o=rtbufsize=64M,video_size=" + c.size + ",framerate=" + str(c.fps) + ",pixel_format=" + c.input_codec,
            "av://dshow:video=" + device_token(c.video)]
    p = sp.Popen(args, stdout=sp.DEVNULL, stderr=sp.DEVNULL, creationflags=CREATE_FLAGS)
    try:
        deadline = time.monotonic() + 12
        ready = False
        while p.poll() is None and time.monotonic() < deadline:
            text = log_path.read_text(errors="replace") if log_path.exists() else ""
            if "VO: [null]" in text:
                ready = True
                break
            time.sleep(.1)
        if not ready:
            return {"case": "second-device-open", "error": "First mpv preview did not become ready", "folder": str(folder)}
        result = record(c, folder, "second-device-open")
        result["first_preview_alive_after_recording"] = p.poll() is None
        return result
    finally:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=5)


def stream_bridge(c, root):
    """Test mpv stream-record -> loopback TCP -> FFmpeg, without a raw disk file.

    Listen on loopback only. The URL ends in .mkv so mpv can infer the muxer.
    This is an experiment: encoder backpressure can stall mpv's demuxer.
    """
    folder = Path(root) / ("bridge-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # There is a small port reservation race. A bind failure is reported, never ignored.
    receive = f"tcp://127.0.0.1:{port}?listen=1&listen_timeout=8000&timeout=8000000"
    ffargs = [c.ffmpeg, "-hide_banner", "-v", "warning", "-n", "-progress", str(folder / "progress.txt"), "-thread_queue_size", "16",
              "-probesize", "4M", "-analyzeduration", "0", "-f", "matroska", "-i", receive]
    if c.audio:
        ffargs += ["-f", "dshow", "-i", "audio=" + device_token(c.audio)]
    ffargs += ["-map", "0:v:0"] + (["-map", "1:a:0"] if c.audio else [])
    ffargs += ["-t", str(c.seconds), "-c:v", c.encoder, "-b:v", "12M", "-pix_fmt", "yuv420p",
               "-r", str(c.fps), "-fps_mode", "cfr",
               "-c:a", "aac", "-b:a", "192k", str(folder / "record.mkv")]
    mpvargs = [c.mpv, "--no-config", "--load-scripts=no", "--vo=null", "--ao=null", "--untimed",
               "--no-terminal", "--length=" + str(c.seconds + 2),
               "--log-file=" + str(folder / "mpv.log"),
               "--stream-record=" + f"tcp://127.0.0.1:{port}/record.mkv",
               "--cache=no", "--container-fps-override=" + str(c.fps),
               "--demuxer-lavf-o=rtbufsize=64M,video_size=" + c.size + ",framerate=" + str(c.fps) + ",pixel_format=" + c.input_codec,
               "av://dshow:video=" + device_token(c.video)]
    save_json(folder / "commands.json", {"ffmpeg": ffargs, "mpv": mpvargs})
    with (folder / "ffmpeg.log").open("wb") as log:
        ff = sp.Popen(ffargs, stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=log, creationflags=CREATE_FLAGS)
        mp = None
        try:
            time.sleep(.5)
            mp = sp.Popen(mpvargs, stdout=sp.DEVNULL, stderr=sp.DEVNULL, creationflags=CREATE_FLAGS)
            try:
                ff.wait(timeout=c.seconds + 20)
            except sp.TimeoutExpired:
                ff.kill()
                ff.wait(timeout=5)
        finally:
            for child in (mp, ff):
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
    output = folder / "record.mkv"
    result = {"case": "mpv-stream-record-tcp", "exit": ff.returncode,
              "folder": str(folder), "files": [inspect(c, output)] if output.exists() else []}
    result["progress"] = (folder / "progress.txt").read_text(errors="replace") if (folder / "progress.txt").exists() else ""
    mpv_log = (folder / "mpv.log").read_text(errors="replace") if (folder / "mpv.log").exists() else ""
    result["device_drop_messages"] = mpv_log.count("frame dropped!")
    save_json(folder / "result.json", result)
    return result


def mpv_mjpeg(c, root):
    """Check whether a generic lavf option actually negotiates MJPEG in mpv.

    FFmpeg CLI's -vcodec is not automatically an mpv/AVOption equivalent.
    Check the recorded codec instead of assuming the option was honoured.
    """
    folder = Path(root) / ("mpv-mjpeg-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    output = folder / "record.mkv"
    args = [c.mpv, "--no-config", "--load-scripts=no", "--vo=null", "--ao=null", "--untimed",
            "--cache=no", "--no-terminal", "--length=3", "--log-file=" + str(folder / "mpv.log"),
            "--stream-record=" + str(output), "--container-fps-override=" + str(c.fps),
            "--demuxer-lavf-o=rtbufsize=64M,video_size=" + c.size + ",framerate=" + str(c.fps) + ",video_codec=mjpeg",
            "av://dshow:video=" + device_token(c.video)]
    save_json(folder / "command.json", args)
    rc, _, err = run(args, 15)
    media = inspect(c, output) if output.exists() else None
    result = {"case": "mpv-mjpeg-option", "exit": rc, "error": err, "folder": str(folder),
              "files": [media] if media else [],
              "negotiated_mjpeg": bool(media and any(s.get("codec_name") == "mjpeg" for s in media.get("streams", [])))}
    save_json(folder / "result.json", result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ffmpeg", required=True)
    p.add_argument("--ffprobe", default="ffprobe.exe")
    p.add_argument("--mpv", required=True)
    p.add_argument("--video", required=True)
    p.add_argument("--audio", default="")
    p.add_argument("--size", default="1920x1080")
    p.add_argument("--fps", type=float, default=60)
    p.add_argument("--input-codec", default="yuyv422")
    p.add_argument("--encoder", choices=["auto", *ENCODERS], default="auto")
    p.add_argument("--output", default=str(Path(__file__).parent / "results" / "integration"))
    p.add_argument("--group", choices=["device", "bridge", "mpv-mjpeg", "containers", "safety", "preview-close", "all"], default="all")
    ns = p.parse_args()
    c = Config(ffmpeg=str(Path(shutil.which(ns.ffmpeg) or ns.ffmpeg).resolve()),
               ffprobe=str(Path(shutil.which(ns.ffprobe) or ns.ffprobe).resolve()),
               mpv=str(Path(shutil.which(ns.mpv) or ns.mpv).resolve()), video=ns.video, audio=ns.audio,
               size=ns.size, fps=ns.fps, input_codec=ns.input_codec, encoder=ns.encoder)
    c = choose_encoder(c)
    root = Path(ns.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    report = []

    def add(label, fn):
        print("Testing " + label, flush=True)
        try:
            result = fn()
            result["experiment"] = label
        except Exception as error:
            result = {"experiment": label, "error": str(error)}
        report.append(result)
        save_json(root / (ns.group + "-results.json"), report)
        print(label + ": " + json.dumps({k: result[k] for k in ("exit", "valid_media", "reason", "bytes", "error") if k in result}), flush=True)

    if ns.group in ("device", "all"):
        add("raw-baseline-3s", lambda: record(dataclasses.replace(c, encoder="raw-baseline", seconds=3), root, "raw-baseline"))
        add("single-owner-raw-preview", lambda: record(dataclasses.replace(c, preview=True, headless=True), root, "raw-preview"))
        add("single-owner-mjpeg-preview", lambda: record(dataclasses.replace(c, input_codec="mjpeg", preview=True, headless=True), root, "mjpeg-preview"))
        add("second-device-open", lambda: shared_device(c, root))
        add("video-only", lambda: record(dataclasses.replace(c, audio=""), root, "video-only"))
    if ns.group in ("bridge", "all"):
        add("mpv-stream-record-tcp", lambda: stream_bridge(c, root))
    if ns.group in ("mpv-mjpeg", "all"):
        add("mpv-mjpeg-option", lambda: mpv_mjpeg(c, root))
    if ns.group in ("containers", "all"):
        synthetic = dataclasses.replace(c, synthetic=True, seconds=7)
        for container in ("mkv", "mp4", "fragmented", "segments"):
            for interrupt in (False, True):
                label = container + ("-interrupted" if interrupt else "-normal")
                conf = dataclasses.replace(synthetic, container=container, kill_after=5 if interrupt else 0)
                add(label, lambda conf=conf, label=label: record(conf, root, label))
        # Remux a normally completed MKV into MP4, preserving compressed packets.
        normal = next((r for r in report if r.get("experiment") == "mkv-normal" and r.get("valid_media")), None)
        if normal:
            source = Path(normal["folder"]) / "record.mkv"
            target = source.with_name("remuxed.mp4")
            def remux():
                rc, _, err = run([c.ffmpeg, "-hide_banner", "-v", "error", "-n", "-i", source,
                                  "-map", "0", "-c", "copy", "-movflags", "+faststart", target])
                return {"exit": rc, "error": err, "files": [inspect(c, target)] if target.exists() else []}
            add("mkv-to-mp4-remux", remux)
    if ns.group in ("safety", "all"):
        synthetic = dataclasses.replace(c, synthetic=True, seconds=10)
        add("early-stop", lambda: record(dataclasses.replace(synthetic, stop_after=3), root, "early-stop"))
        add("size-limit", lambda: record(dataclasses.replace(synthetic, max_mb=1), root, "size-limit"))
        add("low-space-refusal", lambda: record(dataclasses.replace(synthetic, reserve_mb=10**10), root, "low-space"))
        add("invalid-device", lambda: record(dataclasses.replace(c, video="Nonexistent recording lab device"), root, "invalid-device"))
        def runtime_low_space():
            real_disk_usage = shutil.disk_usage
            started = time.monotonic()
            def disk_usage(path):
                usage = real_disk_usage(path)
                return usage._replace(free=0) if time.monotonic() - started > 3 else usage
            with patch("recording_lab.shutil.disk_usage", side_effect=disk_usage):
                return record(synthetic, root, "runtime-low-space")
        add("runtime-low-space-simulated", runtime_low_space)
        def stop_file_and_isolation():
            sentinel = root / ("stop-" + uuid.uuid4().hex)
            unrelated = sp.Popen([c.ffmpeg, "-v", "error", "-re", "-f", "lavfi", "-i",
                                  "sine=frequency=1000", "-t", "30", "-f", "null", "-"],
                                 stdout=sp.DEVNULL, stderr=sp.DEVNULL, creationflags=CREATE_FLAGS)
            timer = threading.Timer(3, sentinel.touch)
            try:
                timer.start()
                result = record(dataclasses.replace(synthetic, stop_file=str(sentinel)), root, "stop-file")
                result["unrelated_ffmpeg_survived"] = unrelated.poll() is None
                return result
            finally:
                timer.cancel()
                timer.join()
                sentinel.unlink(missing_ok=True)
                if unrelated.poll() is None:
                    unrelated.kill()
                unrelated.wait(timeout=5)
        add("stop-file-and-process-isolation", stop_file_and_isolation)
    if ns.group in ("safety", "preview-close", "all"):
        synthetic = dataclasses.replace(c, synthetic=True, seconds=10)
        def preview_closed():
            real_popen = sp.Popen
            timers = []
            def start(args, **kwargs):
                child = real_popen(args, **kwargs)
                if args[0] == c.mpv:
                    timer = threading.Timer(3, lambda: child.terminate() if child.poll() is None else None)
                    timer.start()
                    timers.append(timer)
                return child
            try:
                with patch("recording_lab.sp.Popen", side_effect=start):
                    return record(dataclasses.replace(synthetic, preview=True, headless=True), root, "preview-closed")
            finally:
                for timer in timers:
                    timer.cancel()
                    timer.join()
        add("preview-closed", preview_closed)
    print("Results: " + str(root / (ns.group + "-results.json")))


if __name__ == "__main__":
    main()
