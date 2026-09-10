"""Export allowlisted measurements, excluding media, paths, commands and device IDs.

Usage: python export_summary.py results summary.json
Review the resulting file before publishing it. Full logs remain local.
"""
import argparse
import json
from pathlib import Path


def summarize(result):
    safe = {k: result[k] for k in (
        "case", "experiment", "exit", "reason", "wall_seconds", "preview_exit", "bytes",
        "valid_media", "device_drop_messages", "negotiated_mjpeg", "unrelated_ffmpeg_survived",
        "first_preview_alive_after_recording", "skipped") if k in result}
    if "error" in result:
        safe["has_error"] = bool(result["error"])
    safe["settings"] = {k: result.get("config", {})[k] for k in (
        "size", "fps", "seconds", "input_codec", "encoder", "container", "bitrate", "rtbuf_mb",
        "synthetic", "preview", "headless", "stop_after", "kill_after", "reserve_mb", "max_mb")
        if k in result.get("config", {})}
    safe["files"] = [{k: f[k] for k in ("file", "bytes", "probe_exit", "decode_exit", "duration", "streams") if k in f}
                     for f in result.get("files", [])]
    metrics = dict(line.split("=", 1) for line in result.get("progress", "").splitlines() if "=" in line)
    safe["last_progress"] = {k: metrics[k] for k in ("frame", "fps", "dup_frames", "drop_frames", "speed", "out_time") if k in metrics}
    return safe


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", type=Path)
    p.add_argument("output", type=Path)
    ns = p.parse_args()
    capabilities_file = ns.results / "capabilities.json"
    capabilities = json.loads(capabilities_file.read_text(encoding="utf-8-sig")) if capabilities_file.exists() else {}
    summary = {"note": "Measurements only. See research report for expected failures and acceptance limits.",
               "ffmpeg_version": capabilities.get("ffmpeg_version"),
               "encoder_availability": {name: {k: value[k] for k in ("listed", "works")}
                                        for name, value in capabilities.get("encoders", {}).items()}, "cases": []}
    sources = [ns.results / "matrix.json"] + sorted((ns.results / "integration").glob("*-results.json"))
    for source in sources:
        if source.exists():
            for result in json.loads(source.read_text(encoding="utf-8-sig")):
                # The early device run also contained the untuned bridge. Export
                # the separately rerunnable bridge group instead when present.
                if source.name == "device-results.json" and result.get("experiment") == "mpv-stream-record-tcp":
                    continue
                if source.name == "safety-results.json" and result.get("experiment") == "preview-closed" and (source.parent / "preview-close-results.json").exists():
                    continue
                summary["cases"].append(summarize(result))
    for name in ("long-run", "moving-mjpeg", "moving-software", "raw-buffer-128", "moving-mjpeg-copy"):
        source = ns.results / (name + "-console.txt")
        if source.exists():
            result = json.loads(source.read_text(encoding="utf-8-sig"))
            result["experiment"] = name
            summary["cases"].append(summarize(result))
    ns.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(summary['cases'])} cases to {ns.output}")


if __name__ == "__main__":
    main()
