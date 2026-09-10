# Storage-safe recording investigation

Branch: `research/storage-safe-recording`. Based on `collaboration`, including the device-scan fix. Date: 2026-09-10.

## Finding

Raw temporary files are an implementation choice, not a capture-card requirement. Live H.264 + AAC recording and copying the card's MJPEG stream both work on the tested device. The main constraint is sharing capture with the preview: this card rejects a second video open while mpv owns it.

The [recording lab](../tools/recording-lab/README.md) makes each alternative reproducible without replacing the normal player. Results below are local measurements, not guarantees for other cards. No captured media or private device inventory is checked in.

## Existing implementation

`scripts/autocompress.lua` uses mpv `stream-record` for a video-only MKV, starts a second process for PCM audio, waits for recording to finish, then runs x264 `veryfast` / CRF 23 and AAC 192 kbps to create MP4. A nominal 30-second recording captures 33 seconds before trimming. Temporary files remain until conversion succeeds.

At 1920x1080, 60 fps, YUYV422: `1920 * 1080 * 2 * 60` = 248,832,000 bytes/second. Thus 33 seconds of raw video is approximately 8.21 GB, before audio and the final output. The `.mkv` extension specifies a container, not compression.

Related correctness issues to address before replacing the production recorder:

- `taskkill /IM ffmpeg.exe` and `ffprobe.exe` can terminate unrelated work.
- Fixed `record.mkv` / `audio_temporal.wav` names and delayed deletion can collide with a new session. `is_processing` becomes false before the cleanup timer fires.
- On manual early stop, finalisation still uses the configured target duration rather than measured elapsed duration.
- Independent video/audio start times and fixed sleeps do not establish synchronisation.
- No free-space guard exists; a duration limit alone does not bound bytes across formats/resolutions.

## Test environment

- Windows, USB3.0 Capture, Digital Audio Interface (USB3.0 Capture).
- NVIDIA RTX 4070. FFmpeg's NVENC and software x264 initialize successfully.
- QSV is compiled in but reports unsupported MFX initialization; AMF is compiled in but its runtime DLL is unavailable. Neither is a usable fallback on this machine.
- The card advertises YUYV422 and MJPEG at 1080p up to approximately 60 fps, and at 1440p up to 30 fps. No native H.264 or HEVC mode is advertised.
- Short card tests used a mostly static console menu. Container/safety tests used paced 1080p60 synthetic motion and a 48 kHz sine wave. Static-menu compression figures must not be extrapolated to gameplay.
- Preview transport tests are headless, with the player's normal scripts/config disabled. They do not measure display latency or shader behaviour.

## Short card results

All completed recordings in this table were inspected with ffprobe and decoded with FFmpeg. Sizes are decimal MB. H.264 tests request target 12 Mbps/max 24 Mbps, not strict constant bitrate. Different card modes and sequential samples are not a matched-quality benchmark.

| Input / method | Requested duration | Output size | Video packets | Outcome |
|---|---:|---:|---:|---|
| YUYV422 raw baseline + PCM | 3 s | 751.18 MB | See local report | Decodable; illustrates temporary-storage cost |
| YUYV422 to software H.264 + AAC | 5 s | 4.49 MB | 301 | Decodable |
| YUYV422 to NVENC H.264 + AAC | 5 s | 1.80 MB | 301 | Decodable |
| MJPEG to software H.264 + AAC | 5 s | 1.29 MB | 298 | Decodable |
| MJPEG to NVENC H.264 + AAC | 5 s | 0.78 MB | 298 | Decodable |
| MJPEG copied, AAC audio | 5 s | 30.56 MB | 298 | Decodable; no video re-encoding |
| Raw input, NVENC recording plus unencoded mpv preview pipe | 5 s | 1.80 MB | See local report | Both processes completed successfully |
| MJPEG input, NVENC recording plus unencoded mpv preview pipe | 5 s | 0.77 MB | See local report | Both processes completed successfully |

Short encoded outputs were approximately 5.02–5.08 seconds. Progress did not report FFmpeg duplication/dropping for the initial direct-recording matrix. That does not prove that the source/driver never repeats or loses a frame. Audio start offsets and codec priming also require a proper sync-marker test; merely finding an audio stream is insufficient.

The card advertises limited-range raw video and full-range MJPEG. Recorded metadata also differs by mode, and MJPEG conversion emitted a pixel-format/range warning. Preserve/validate colour metadata; do not silently hardcode one colour range for all cards. The tested H.264 output reduces 4:2:2 input to 4:2:0, which is a quality/compatibility choice.

### Longer moving-content check

After switching the source to moving content, raw input -> NVENC H.264/AAC with the unencoded headless preview branch completed a requested 60-second session:

- 92,281,001 bytes (92.28 MB), compared with approximately 14.93 GB for 60 seconds of raw YUYV422.
- 60.062 seconds reported duration; 3,598 video packets versus nominal 3,600.
- Recorder and preview both exited successfully; output probed and decoded successfully.
- FFmpeg's progress reported zero duplicated/dropped output frames, **but the DirectShow log contained three device frame-drop warnings**. The two counters describe different stages. This is not a zero-loss result.
- An audio-level analysis confirmed non-silent audio. Perceptual audio/video sync was not validated with a marker.

The player was closed before testing because its existing preview held the capture device. This also reproduced the exclusive-open constraint during ordinary use.

Additional moving-source comparisons, all with an unencoded headless preview:

| Input / encoder / input buffer | Duration | Bytes | Video packets | Driver drop messages |
|---|---:|---:|---:|---:|
| MJPEG / NVENC / 64 MB | 30 s | 46,886,487 | 1,801 | 0 |
| YUYV422 / software x264 / 64 MB | 30 s | 44,075,372 | 1,798 | 3 |
| YUYV422 / NVENC / 128 MB | 30 s | 46,716,394 | 1,801 | 0 |

All three decoded successfully, included audio, and reported zero output duplication/drop counters. Increasing the raw input buffer removed driver-drop warnings in this run. It is exposed as `--rtbuf-mb`, not silently selected for all devices; it changes memory use and potential buffering latency. Playback latency was not measured. Native input sample rate here was 44.1 kHz stereo, reinforcing that a generic implementation must not assume 48 kHz solely from a USB device label.

A five-second moving-source MJPEG-copy recording used **68,461,741 bytes**, decoded successfully, and logged no device drops. That is appreciably larger than the static-menu MJPEG sample (30.56 MB), illustrating why native compressed copy cannot promise a fixed file size.

## Architecture comparisons

### 1. Record compressed input with the existing approach

If a card delivers MJPEG/H.264/HEVC, copying that compressed stream avoids raw temporary video. FFmpeg successfully negotiated MJPEG and copied it on this card. Native H.264/HEVC are not available on this card and remain hardware-unverified, with mode detection and copy command support provided in the lab.

Important qualification: requesting MJPEG in **mpv** is not proven to be a simple config change. The candidate `--demuxer-lavf-o=video_codec=mjpeg` failed to open this input, while FFmpeg CLI `-vcodec mjpeg` worked. Do not ship the guessed option. A capture helper that explicitly negotiates the codec is a tested route. MJPEG still needs a later conversion for a small H.264 MP4; direct native H.264 copy could avoid video conversion entirely on supporting cards.

### 2. Start a separate recorder alongside mpv

Tested with a ready headless mpv owning the video device before opening FFmpeg. The second open failed with "Could not run graph (sometimes caused by a device already in use by other application)". Keep this as a device-dependent option, not the universal default. A friendly name or USB3 label cannot predict multi-client support.

### 3. One capture owner, separate preview and recording outputs

FFmpeg opens video and audio together. It encodes the recording directly to MKV while forwarding the source video/audio through an OS pipe in NUT to mpv. Raw and MJPEG input both worked in short headless tests. No raw file is written; the source remains unencoded on the preview branch.

This is the leading architecture to develop. Production integration requires explicit preview buffering/drop policy, recording start/stop without restarting the preview, device-loss handling, IPC/control lifecycle, and validation under GPU/shader load. The current prototype starts a bounded recording session and ends its preview with that session. It is not yet a drop-in implementation of the player's toggle button.

### 4. mpv stream-record to an encoder through memory/loopback transport

A loopback TCP bridge avoids a raw disk file and avoids opening video twice. It did create decodable H.264/AAC output, but the tuned five-second test logged **307 device frame-drop messages and 195 duplicated output frames**. Nominal 60 fps metadata and 300 output packets therefore concealed poor source-frame delivery. This approach is **not suitable as implemented**. Preserve the experiment for inspection; do not call it a successful low-latency recorder merely because its output plays.

The bridge uses bounded queues, a 64 MB DirectShow buffer, and a fixed timeout. Encoder/audio-input startup and backpressure still affect mpv's demuxing. A Windows named pipe changes transport, but does not by itself solve those scheduling/backpressure problems; it has not been tested separately.

## Containers and interruption

Seven-second synthetic recordings, with forced interruption around five wall-clock seconds:

| Container | Normal completion | Forced interruption |
|---|---|---|
| MKV | Probe/decode succeeded | Partial media remained decodable in this run |
| Standard MP4 | Probe/decode succeeded | Unreadable: not properly finalized |
| Fragmented MP4 | Probe/decode succeeded | Partial media remained decodable in this run |
| Two-second MKV segments | All segments decoded | Earlier two segments decoded; final segment was empty/unreadable |

This is one controlled interruption per mode, not proof against every failure point or sudden power loss. Segmentation preserves earlier completed segments but does not cap total storage. A completed MKV was remuxed to standard MP4 with `-c copy` and the result decoded successfully. SHA-256 stream hashes of both copied video and audio matched before and after remuxing. Remuxing temporarily needs space for both compressed files; it does not create raw intermediates.

## Storage and process tests

- Graceful early stopping produced decodable media.
- A 1 MiB aggregate output threshold requested stop; the result was approximately 3.1 MB after buffers drained. This is a soft threshold, not a hard byte limit.
- An impossible free-space reserve refused capture before starting FFmpeg.
- Simulated runtime low-space triggered graceful stop with decodable media; no real disk was filled.
- Invalid device selection reported failure instead of success.
- Stop-file control produced decodable media while a separate FFmpeg test process remained alive.
- Deliberately closing the owned headless preview during a synthetic recording triggered recorder shutdown. FFmpeg reported a pipe error, but its partial MKV decoded successfully. This is recovery from preview failure, not seamless recording continuation after preview closes.
- Unit coverage includes alternate device formats/rates, range parsing and deduplication, missing modes, explicit raw-copy rejection, video-only maps, input codec placement, single-owner preview construction, storage boundaries, refusing to spawn on low space, unique output directories, and container selection.

## Recommended development order

1. Agree on the capture-owner architecture with the author. Retain the old recorder until the new path passes preview and sync acceptance tests.
2. Use discovered device modes, not USB branding, to select raw versus compressed input. Expose an explicit format override and inspect the negotiated stream.
3. Default experimental recording to H.264/AAC in MKV, with a tested hardware encoder and software fallback. Keep native compressed copy available where reported; treat MJPEG as an optional intermediate/quality tradeoff.
4. Add session-owned process control, unique paths, measured elapsed duration, free-space monitoring, and success-only cleanup to production integration.
5. Add fragmented MP4 and segmented MKV as optional durability modes, with lossless remux to ordinary MP4 where needed.
6. Verify 1080p60 moving content, audio/video sync markers, shader/GPU load, longer recordings, unplug/replug, graceful user stop, repeat recording, and preview latency. Repeat on Intel/AMD and additional raw/MJPEG/native-H.264 capture cards.

Lossless FFV1, H.265/AV1 transcoding, and screen/window capture are further possibilities, but are outside the implemented comparison: FFV1 storage depends heavily on image content, H.265/AV1 add hardware/compatibility considerations, and screen capture changes what is recorded. None is needed to eliminate raw files for the demonstrated H.264 path.

## Primary references

- [mpv stream-record](https://mpv.io/manual/master/#options-stream-record): records demuxed input; container choice alone is not encoding.
- [FFmpeg DirectShow](https://ffmpeg.org/ffmpeg-devices.html#dshow): format discovery, device selection, audio/video input support.
- [FFmpeg fragmentation](https://ffmpeg.org/ffmpeg-formats.html#Fragmentation): interrupted fragmented MP4 versus ordinary MP4.
- [FFmpeg segment muxer](https://ffmpeg.org/ffmpeg-formats.html#segment_002c-stream_005fsegment_002c-ssegment): keyframe-dependent segment boundaries.
