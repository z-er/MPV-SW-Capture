# Storage-safe recording lab

This is an **opt-in research prototype**, not a replacement for the player's recording script. It makes the alternatives reproducible on different Windows capture devices. It does not change Setup, device selections, shaders, keybindings, or the existing raw recorder.

Requires Python 3.10+, FFmpeg, ffprobe, and mpv for preview experiments. Python uses only its standard library. ffprobe is a separate executable and is not included in this project's installer. Use a trusted FFmpeg distribution, preferably matching the FFmpeg executable used for recording. No package installation is required by these scripts.

## Start here

Run from the repository root, with the capture player and other applications using the card closed. The lab cannot guarantee that a DirectShow driver supports multiple simultaneous opens.

```powershell
$ffmpeg = 'C:\path\to\ffmpeg.exe'
$ffprobe = 'C:\path\to\ffprobe.exe'
$mpv = 'C:\path\to\mpv.exe'
$lab = '.\tools\recording-lab\recording_lab.py'

# List local devices, inspect the chosen card, and actually initialize encoders.
python $lab probe --ffmpeg $ffmpeg --ffprobe $ffprobe --video 'Your capture device'

# Compare all advertised input codecs/pixel formats at this resolution/rate.
# Include --audio only when recording audio is wanted.
python $lab matrix --ffmpeg $ffmpeg --ffprobe $ffprobe `
  --video 'Your capture device' --audio 'Your capture audio device' `
  --size 1920x1080 --fps 60 --seconds 5
```

`capabilities.json` contains the original device/mode output, parsed ranges, FFmpeg version, and encoder initialization failures. `matrix.json` contains the individual test outcomes. Mode ranges are candidates, not guarantees: successfully opening and recording the selected mode is the actual compatibility test. A device that lists H.264/HEVC is tested with native stream copy; missing modes are reported as skipped. Different raw formats such as NV12 are included when advertised. No mode is inferred from a device's friendly name.

Use a listed alternative device name as `--video`/`--audio` if friendly names collide or contain a colon. DirectShow uses colons as separators and does not support quoting them inside device names; the lab rejects these names with a specific message instead of silently opening the wrong input. Spaces, apostrophes, and alternative-ID backslashes are preserved. Do not change global device settings just to satisfy this test. The lab does not select arbitrary fallback resolutions or change a driver's defaults after an open fails.

## Recording options

```powershell
# Live H.264 + AAC, no raw file. Auto probes NVENC, QSV, AMF, then software x264.
python $lab record --ffmpeg $ffmpeg --ffprobe $ffprobe `
  --video 'Your capture device' --audio 'Your capture audio device' `
  --input-codec yuyv422 --encoder auto --seconds 30

# Copy the card's compressed stream. Only valid if it advertises this mode.
python $lab record --ffmpeg $ffmpeg --ffprobe $ffprobe `
  --video 'Your capture device' --input-codec mjpeg --encoder copy --seconds 5

# One capture owner: encode to disk and feed unencoded video/audio to mpv.
python $lab record --ffmpeg $ffmpeg --ffprobe $ffprobe --mpv $mpv `
  --video 'Your capture device' --audio 'Your capture audio device' `
  --preview --seconds 30

# Headless transport check, not a display-latency benchmark.
# Add --headless to the preceding command.
```

Encoder choices: `auto`, `libx264`, `h264_nvenc`, `h264_qsv`, `h264_amf`, `copy`, `raw-baseline`. Automatic selection probes hardware before recording and falls back to software if initialization fails. It does **not** restart a failed recording mid-session or discard a partial result. An explicit encoder choice fails visibly if unavailable.

Container choices (`--container`):

| Value | Purpose |
|---|---|
| `mkv` | Default; compressed media written directly, one-second cluster limit |
| `fragmented` | Fragmented MP4; better interruption recovery than ordinary MP4 |
| `segments` | Two-second MKV segments, aligned to available keyframes |
| `mp4` | Standard MP4; included for compatibility and interruption comparisons |

Segments limit the damage from an interrupted final segment. **They do not bound total storage** and are not a rolling buffer. Native stream-copy segment boundaries depend on the card's GOP; two seconds is a target, not a promise.

`raw-baseline` is deliberately explicit and restricted to MKV. At 1080p60 YUYV422 it writes about 249 MB/s. Use only a short sample with enough space. MJPEG is lossy and variable-size; native H.264/HEVC copy preserves the source's quality and bitrate. H.264 encoding here uses a 12 Mbps target, a 24 Mbps maximum rate setting, AAC 192 kbps, and 4:2:0 output. x264 uses `veryfast`; NVENC uses `p4`. These are comparison settings, not established quality recommendations. The current product uses CRF 23 after recording, so these numbers are not a quality-matched benchmark against its final MP4.

## Storage and process controls

- Each recording gets a new timestamp/UUID directory. `-n` prevents overwriting existing media.
- `--reserve-mb 512` checks free space before opening the device and every 100 ms while recording.
- `--max-mb 2048` requests a graceful stop when accumulated media reaches the threshold, including all segments.
- These are **soft stop thresholds**. Encoder/muxer buffers and shutdown can add bytes after the threshold. Keep a generous reserve; do not use a threshold as an exact disk quota. In a 1 MiB threshold test, final output was about 3 MiB after buffered data flushed.
- Duration is bounded, with an additional 15-second wall-clock watchdog and a five-second shutdown grace period.
- `--stop-file C:\path\to\unique-stop-file` lets another process request stop by creating the file. Use a fresh path per recording. The lab does not delete the caller's stop file.
- `--stop-after 3` tests graceful early stopping. `--kill-after 5` deliberately terminates only this lab's recorder to test crash recovery.
- Only child process handles created by the lab are terminated. No `taskkill /IM`, no process-name matching, and no deletion of failed media.
- `Ctrl+C` cleans up owned processes but may force termination; use the stop file for graceful stopping.

Preview uses a bounded OS pipe carrying NUT video/audio. It avoids a second device open and avoids compression in the preview branch. **A slow preview can still backpressure the recorder**; the production design needs a separately bounded preview queue with an explicit drop policy. Headless results do not establish latency with GPU rendering, shaders, or games using the GPU.

`--rtbuf-mb` controls the DirectShow input buffer (default 64 MB). Higher values can absorb startup stalls at the cost of additional memory and potential latency. Choose this with the input byte rate in mind; the same buffer represents much less time for raw 4K than for compressed MJPEG. Driver buffer-drop messages are collected separately from FFmpeg's output duplication/drop counters.

## Repeat the integration experiments

```powershell
python .\tools\recording-lab\run_experiments.py `
  --ffmpeg $ffmpeg --ffprobe $ffprobe --mpv $mpv `
  --video 'Your capture device' --audio 'Your capture audio device' --group all

python -m unittest discover -s .\tools\recording-lab -p 'test_*.py'
```

Groups can be run individually:

- `device`: short raw baseline, raw/MJPEG single-owner preview, second-device-open check, video-only recording. Defaults to 1080p60 YUYV422/MJPEG. Override `--size`, `--fps`, `--input-codec`, and `--encoder` for another card. Unsupported MJPEG and simultaneous-open cases may fail by design; inspect the report. Use the matrix for all advertised formats at the requested size/rate.
- `containers`: paced synthetic motion/audio; normal and forcibly interrupted MKV, MP4, fragmented MP4, segments; lossless remux to MP4.
- `safety`: early stop, size threshold, preflight low-space refusal, simulated runtime low-space stop, invalid device, stop-file control, unrelated-process survival, preview closure. Use `preview-close` to rerun only the last case.
- `bridge`: mpv `stream-record` over loopback TCP to FFmpeg. This alternative had severe frame loss on the development card. Kept for investigation, **not recommended**.
- `mpv-mjpeg`: checks a candidate generic lavf option and verifies the negotiated codec. The tested `video_codec=mjpeg` option did not open the device in this mpv build. FFmpeg CLI `-vcodec mjpeg` is not automatically an equivalent mpv setting.

The integration runner records both expected failures and successful cases and exits after writing its report. Its exit code is not an overall pass/fail verdict. The normal `record` command returns failure for a failed child or invalid media. `valid_media` means that ffprobe found packets in the expected streams and FFmpeg decoded them; it does **not** mean complete frame delivery, correct colour, or perceptual audio sync. Inspect progress, device-drop messages, packet counts, and the media itself.

The short TCP bridge and mpv-option probes are isolated, watchdog-bounded experiments; they do not use the full recorder's runtime storage guard. The bridge binds to loopback only, not a LAN interface. Its ephemeral-port reservation has a small bind race, which is reported as failure.

## Sharing findings

`results/` is ignored by Git. It contains local recordings, device identifiers, command lines, paths, logs, and JSON measurements. Review/redact those before sharing. The checked-in [research report](../../docs/storage-safe-recording.md) contains aggregate findings only; recordings are not uploaded.

`export_summary.py results summary.json` exports an allowlist of measurement fields, omitting local paths, commands, device names and error log text. Review that output too before publishing it. The development measurements are available in [storage-safe-recording-results.json](../../docs/storage-safe-recording-results.json).

For another card, report: advertised and actually negotiated format/size/rate, driver/device identity, encoder initialization, whether simultaneous device opens work, packet/drop counts, output bytes/duration, source content type, and visible preview/audio behaviour. Include a long moving-content test with a visible/audible sync marker before treating a mode as production-ready.
