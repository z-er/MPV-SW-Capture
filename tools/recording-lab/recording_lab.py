"""Opt-in Windows recording experiments; Python 3.10+, FFmpeg, ffprobe.

Does not modify the player's settings or use process-name-wide termination.
Media and local device diagnostics stay in the ignored results directory.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess as sp
import time
import uuid

CREATE_FLAGS = sp.CREATE_NO_WINDOW if os.name == "nt" else 0
ENCODERS = ("libx264", "h264_nvenc", "h264_qsv", "h264_amf")
COMPRESSED_INPUTS = ("mjpeg", "h264", "hevc")


def save_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def run(args, timeout=20):
    try:
        p = sp.run(list(map(str, args)), capture_output=True, timeout=timeout,
                   creationflags=CREATE_FLAGS)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except sp.TimeoutExpired:
        return -1, "", f"Timed out after {timeout} seconds"


def parse_modes(text):
    """Keep advertised size/fps ranges, without inventing intermediate modes."""
    pattern = re.compile(
        r"(pixel_format|vcodec)=(\S+)\s+min s=(\d+)x(\d+) fps=([\d.]+)"
        r"\s+max s=(\d+)x(\d+) fps=([\d.]+)")
    modes = []
    for m in pattern.finditer(text):
        mode = dict(kind=m[1], codec=m[2], min_size=[int(m[3]), int(m[4])],
                    max_size=[int(m[6]), int(m[7])], min_fps=float(m[5]), max_fps=float(m[8]))
        if mode not in modes:
            modes.append(mode)
    return modes


def advertised(modes, codec, size, fps):
    # Range membership is only an advertised candidate. Actual opens must still pass.
    return any(m["codec"] == codec and
               all(lo <= n <= hi for lo, n, hi in zip(m["min_size"], size, m["max_size"])) and
               m["min_fps"] - .01 <= fps <= m["max_fps"] + .01 for m in modes)


@dataclasses.dataclass
class Config:
    ffmpeg: str
    ffprobe: str
    video: str = ""
    audio: str = ""
    size: str = "1920x1080"
    fps: float = 60
    seconds: float = 5
    input_codec: str = "yuyv422"
    encoder: str = "h264_nvenc"
    container: str = "mkv"
    bitrate: int = 12
    rtbuf_mb: int = 64
    reserve_mb: int = 512
    max_mb: int = 2048
    synthetic: bool = False
    mpv: str = ""
    preview: bool = False
    headless: bool = False
    stop_after: float = 0
    kill_after: float = 0
    stop_file: str = ""


def input_args(c):
    if c.synthetic:
        # Paced inputs exercise real-time stop/storage behaviour as well as encoding.
        return ["-re", "-f", "lavfi", "-i", f"testsrc2=size={c.size}:rate={c.fps}",
                "-re", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000"]
    if not c.video:
        raise ValueError("Supply --video or --synthetic")
    args = ["-rtbufsize", f"{c.rtbuf_mb}M", "-f", "dshow", "-video_size", c.size,
            "-framerate", str(c.fps)]
    if c.input_codec in COMPRESSED_INPUTS:
        args += ["-vcodec", c.input_codec]
    else:
        args += ["-pixel_format", c.input_codec]
    device = "video=" + device_token(c.video)
    if c.audio:
        device += ":audio=" + device_token(c.audio)
    return args + ["-i", device]


def device_token(name):
    # DirectShow uses strtok, NOT av_get_token: quotes become part of the name.
    # Spaces/apostrophes/backslashes must stay literal. No shell is involved.
    if ":" in name:
        raise ValueError("DirectShow cannot escape ':' in a device name; use its listed alternative device ID")
    return name


def maps(c):
    return ["-map", "0:v:0"] + (["-map", "1:a:0"] if c.synthetic else
                                 ["-map", "0:a:0"] if c.audio else [])


def codec_args(c):
    if c.encoder == "copy":
        if c.synthetic or c.input_codec not in COMPRESSED_INPUTS:
            raise ValueError("copy requires a compressed device input; use raw-baseline explicitly for raw")
        return ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    if c.encoder == "raw-baseline":
        if c.container != "mkv":
            raise ValueError("raw-baseline requires MKV")
        return ["-c:v", "rawvideo", "-pix_fmt", "yuyv422", "-c:a", "pcm_s16le"]
    if c.encoder not in ENCODERS:
        raise ValueError("Unsupported encoder")
    args = ["-c:v", c.encoder, "-pix_fmt", "yuv420p", "-g", str(round(c.fps * 2)),
            "-b:v", f"{c.bitrate}M", "-maxrate", f"{c.bitrate * 2}M",
            "-bufsize", f"{c.bitrate * 2}M", "-c:a", "aac", "-b:a", "192k"]
    if c.encoder == "libx264":
        args += ["-preset", "veryfast"]
    elif c.encoder == "h264_nvenc":
        args += ["-preset", "p4"]
    return args


def output_args(c):
    if c.container == "mkv":
        return ["-cluster_time_limit", "1000", "-allow_raw_vfw", "1", "record.mkv"]
    if c.container == "mp4":
        return ["-movflags", "+faststart", "record.mp4"]
    if c.container == "fragmented":
        return ["-movflags", "+frag_keyframe+empty_moov+default_base_moof", "record.mp4"]
    return ["-f", "segment", "-segment_time", "2", "-reset_timestamps", "1",
            "-segment_format", "matroska", "record-%03d.mkv"]


def command(c):
    args = [c.ffmpeg, "-hide_banner", "-n", "-loglevel", "warning", "-stats_period", "1",
            "-progress", "progress.txt"] + input_args(c)
    args += maps(c) + ["-t", str(c.seconds)] + codec_args(c) + output_args(c)
    if c.preview:
        # Independent unencoded output: one device owner, no video encoder in preview path.
        # OS pipe is bounded; slow/stopped preview CAN backpressure recording in this prototype.
        preview_codecs = ["-c:v", "rawvideo", "-c:a", "pcm_s16le"] if c.synthetic else ["-c", "copy"]
        args += maps(c) + ["-t", str(c.seconds)] + preview_codecs + ["-f", "nut", "pipe:1"]
    return args


def media_files(folder):
    return sorted(p for p in Path(folder).glob("record*") if p.suffix in (".mkv", ".mp4"))


def storage_reason(free_bytes, reserve_bytes, written_bytes, max_bytes):
    if free_bytes < reserve_bytes:
        return "low_disk"
    if written_bytes >= max_bytes:
        return "size_limit"
    return ""


def inspect(c, path):
    rc, out, err = run([c.ffprobe, "-v", "error", "-count_packets", "-show_streams",
                         "-show_format", "-of", "json", path], 30)
    result = {"file": Path(path).name, "bytes": Path(path).stat().st_size, "probe_exit": rc}
    try:
        metadata = json.loads(out)
        result["streams"] = [{k: s[k] for k in (
            "codec_name", "codec_type", "width", "height", "pix_fmt", "avg_frame_rate",
            "start_time", "duration", "nb_read_packets", "sample_rate", "channels", "color_range", "color_space") if k in s}
            for s in metadata.get("streams", [])]
        result["duration"] = metadata.get("format", {}).get("duration")
    except ValueError:
        result["probe_error"] = err[-2000:]
    decode_rc, _, decode_err = run([c.ffmpeg, "-hide_banner", "-v", "error", "-xerror",
                                    "-i", path, "-map", "0", "-f", "null", "-"], 60)
    result.update(decode_exit=decode_rc, decode_error=decode_err[-2000:])
    return result


def choose_encoder(c):
    if c.encoder == "auto":
        available = probe_encoders(c)
        selected = next((e for e in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")
                         if available[e]["works"]), None)
        if selected is None:
            raise ValueError("No working H.264 encoder found")
        c = dataclasses.replace(c, encoder=selected)
    return c


def record(c, root, name="record"):
    c = choose_encoder(c)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    folder = root / (dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + name + "-" + uuid.uuid4().hex[:8])
    folder.mkdir()
    args = command(c)
    save_json(folder / "command.json", {"args": args, "config": dataclasses.asdict(c)})
    reserve = c.reserve_mb * 1024**2
    if shutil.disk_usage(folder).free < reserve + min(c.max_mb, 256) * 1024**2:
        raise ValueError("Insufficient free space above the configured reserve")
    if c.preview and not c.mpv:
        raise ValueError("--preview requires --mpv")
    preview = None
    p = None
    reason = "completed"
    started = time.monotonic()
    stop_at = None
    with (folder / "ffmpeg.log").open("wb") as log, (folder / "preview.log").open("wb") as plog:
        try:
            p = sp.Popen(args, cwd=folder, stdin=sp.PIPE,
                         stdout=sp.PIPE if c.preview else sp.DEVNULL, stderr=log,
                         creationflags=CREATE_FLAGS)
            if c.preview:
                preview_args = [c.mpv, "--no-config", "--load-scripts=no", "--cache=no", "--idle=no",
                                "--keep-open=no", "--terminal=yes"]
                if c.headless:
                    preview_args += ["--vo=null", "--ao=null", "--untimed"]
                preview = sp.Popen(preview_args + ["-"], stdin=p.stdout, stdout=plog, stderr=plog,
                                   creationflags=CREATE_FLAGS)
                p.stdout.close()
            while p.poll() is None:
                elapsed = time.monotonic() - started
                written = sum(f.stat().st_size for f in media_files(folder))
                request = storage_reason(shutil.disk_usage(folder).free, reserve, written, c.max_mb * 1024**2)
                if c.stop_file and Path(c.stop_file).exists():
                    request = request or "stop_file"
                if c.stop_after and elapsed >= c.stop_after:
                    request = request or "early_stop"
                if preview is not None and preview.poll() is not None:
                    request = request or "preview_closed"
                if elapsed > c.seconds + 15:
                    request = request or "timeout"
                if c.kill_after and elapsed >= c.kill_after:
                    reason = "forced_interruption"
                    p.kill()
                    break
                if request and stop_at is None:
                    reason = request
                    stop_at = time.monotonic()
                    try:
                        p.stdin.write(b"q\n")
                        p.stdin.flush()
                    except (OSError, ValueError):
                        pass
                if stop_at is not None and time.monotonic() - stop_at > 5:
                    p.kill()
                    reason += "_forced"
                    break
                time.sleep(.1)
            p.wait(timeout=6)
        finally:
            if p is not None:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=6)
                if p.stdin:
                    p.stdin.close()
                if p.stdout and not p.stdout.closed:
                    p.stdout.close()
            if preview is not None:
                try:
                    preview.wait(timeout=5)
                except sp.TimeoutExpired:
                    preview.kill()
                    preview.wait(timeout=5)
    if p.returncode != 0 and reason == "completed":
        reason = "process_error"
    result = {"case": name, "folder": str(folder), "config": dataclasses.asdict(c),
              "exit": p.returncode, "reason": reason, "wall_seconds": round(time.monotonic() - started, 3),
              "preview_exit": preview.returncode if preview else None,
              "files": [inspect(c, f) for f in media_files(folder)]}
    result["bytes"] = sum(f["bytes"] for f in result["files"])
    result["valid_media"] = bool(result["files"]) and all(
        f["probe_exit"] == 0 and f["decode_exit"] == 0 and
        any(s.get("codec_type") == "video" and int(s.get("nb_read_packets", "0")) > 0
            for s in f.get("streams", [])) and
        (not (c.synthetic or c.audio) or any(
            s.get("codec_type") == "audio" and int(s.get("nb_read_packets", "0")) > 0
            for s in f.get("streams", []))) for f in result["files"])
    result["progress"] = (folder / "progress.txt").read_text(errors="replace") if (folder / "progress.txt").exists() else ""
    result["metrics"] = dict(line.split("=", 1) for line in result["progress"].splitlines() if "=" in line)
    log_text = (folder / "ffmpeg.log").read_text(errors="replace")
    result["device_drop_messages"] = log_text.count("frame dropped!")
    save_json(folder / "result.json", result)
    return result


def probe_encoders(c):
    _, encoder_list, _ = run([c.ffmpeg, "-hide_banner", "-encoders"])
    encoders = {}
    for encoder in ENCODERS:
        if not re.search(r"\b" + re.escape(encoder) + r"\s", encoder_list):
            encoders[encoder] = {"listed": False, "works": False}
            continue
        test = dataclasses.replace(c, encoder=encoder)
        args = [c.ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                f"testsrc2=size={c.size}:rate={c.fps}", "-frames:v", "30"] + codec_args(test) + ["-f", "null", "-"]
        rc, _, err = run(args, 30)
        encoders[encoder] = {"listed": True, "works": rc == 0, "error": err[-2000:]}
    return encoders


def probe(c, root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _, _, devices = run([c.ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
    modes_text = ""
    if c.video:
        _, _, modes_text = run([c.ffmpeg, "-hide_banner", "-list_options", "true", "-f", "dshow", "-i", "video=" + device_token(c.video)])
    _, build, _ = run([c.ffmpeg, "-version"])
    encoders = probe_encoders(c)
    result = {"ffmpeg_version": build.splitlines()[0] if build else "unknown",
              "devices_raw": devices, "modes_raw": modes_text, "modes": parse_modes(modes_text),
              "encoders": encoders}
    save_json(root / "capabilities.json", result)
    return result


def matrix(c, root):
    capabilities = probe(c, root)
    size = list(map(int, c.size.split("x")))
    modes = capabilities["modes"]
    cases = []
    # Include every reported codec/pixel format for the selected size/rate,
    # not just the raw format seen on the development card.
    sources = list(dict.fromkeys([c.input_codec] + [m["codec"] for m in modes] + list(COMPRESSED_INPUTS)))
    for source in sources:
        if any(label.startswith(source + "-") for label, _ in cases):
            continue
        if not c.synthetic and not advertised(modes, source, size, c.fps):
            cases.append((source + "-not-advertised", None))
            continue
        if c.synthetic and source != c.input_codec:
            continue
        for encoder in ENCODERS:
            if capabilities["encoders"][encoder]["works"]:
                cases.append((source + "-" + encoder, dataclasses.replace(c, input_codec=source, encoder=encoder)))
        if source in COMPRESSED_INPUTS:
            cases.append((source + "-copy", dataclasses.replace(c, input_codec=source, encoder="copy")))
    report = []
    for label, config in cases:
        if config is None:
            report.append({"case": label, "skipped": "Mode not advertised at requested size/fps"})
        else:
            print("Testing " + label, flush=True)
            try:
                report.append(record(config, root, label))
            except (ValueError, OSError) as error:
                report.append({"case": label, "error": str(error)})
        save_json(Path(root) / "matrix.json", report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["probe", "record", "matrix"])
    p.add_argument("--ffmpeg", default="ffmpeg.exe")
    p.add_argument("--ffprobe", default="ffprobe.exe")
    p.add_argument("--video", default="")
    p.add_argument("--audio", default="")
    p.add_argument("--size", default="1920x1080")
    p.add_argument("--fps", type=float, default=60)
    p.add_argument("--seconds", type=float, default=5)
    p.add_argument("--input-codec", default="yuyv422", help="Advertised raw pixel format or mjpeg/h264/hevc")
    p.add_argument("--encoder", choices=["auto", *ENCODERS, "copy", "raw-baseline"], default="auto")
    p.add_argument("--container", choices=["mkv", "mp4", "fragmented", "segments"], default="mkv")
    p.add_argument("--bitrate", type=int, default=12, help="Target video Mbps (not a hard file-size guarantee)")
    p.add_argument("--rtbuf-mb", type=int, default=64, help="DirectShow input buffer MB; larger queues may add latency")
    p.add_argument("--reserve-mb", type=int, default=512)
    p.add_argument("--max-mb", type=int, default=2048)
    p.add_argument("--synthetic", action="store_true")
    p.add_argument("--mpv", default="")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--stop-after", type=float, default=0, help="Graceful stop after wall-clock seconds")
    p.add_argument("--kill-after", type=float, default=0, help="Deliberately kill this test child to test container recovery")
    p.add_argument("--stop-file", default="", help="Creating this file requests a graceful stop")
    p.add_argument("--output", default=str(Path(__file__).parent / "results"))
    ns = p.parse_args()
    action, root = ns.action, Path(ns.output).resolve()
    values = vars(ns).copy()
    del values["action"], values["output"]
    if not re.fullmatch(r"[1-9]\d*x[1-9]\d*", ns.size) or not 0 < ns.fps <= 240:
        p.error("Invalid size or frame rate")
    if not 0 < ns.seconds <= 3600 or ns.max_mb <= 0 or ns.reserve_mb < 0 or ns.bitrate <= 0 or not 1 <= ns.rtbuf_mb <= 1024:
        p.error("Invalid duration or storage/bitrate limits")
    for key in ("ffmpeg", "ffprobe", "mpv"):
        if values[key]:
            binary = shutil.which(values[key])
            if not binary:
                p.error(f"Executable not found: {values[key]}")
            values[key] = str(Path(binary).resolve())
    if values["stop_file"]:
        values["stop_file"] = str(Path(values["stop_file"]).resolve())
    c = Config(**values)
    try:
        result = {"probe": probe, "record": record, "matrix": matrix}[action](c, root)
    except (ValueError, OSError) as error:
        p.exit(1, str(error) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if action == "record" and (result["exit"] != 0 or not result["valid_media"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
