import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recording_lab import Config, ENCODERS, advertised, choose_encoder, codec_args, command, device_token, parse_modes, record, storage_reason
from export_summary import summarize


class RecordingLabTests(unittest.TestCase):
    def test_modes_preserve_nonstandard_card_formats_and_ranges(self):
        text = '''
[dshow] pixel_format=nv12 min s=1280x720 fps=29.97 max s=1280x720 fps=59.94
[dshow] pixel_format=nv12 min s=1280x720 fps=29.97 max s=1280x720 fps=59.94 (tv)
[dshow] vcodec=h264 min s=1920x1080 fps=30 max s=1920x1080 fps=60
[dshow] vcodec=mjpeg min s=640x480 fps=10 max s=1920x1080 fps=30
'''
        modes = parse_modes(text)
        self.assertEqual(len(modes), 3)
        self.assertTrue(advertised(modes, "nv12", [1280, 720], 59.94))
        self.assertFalse(advertised(modes, "nv12", [1920, 1080], 60))
        self.assertFalse(advertised(modes, "mjpeg", [1920, 1080], 60))
        self.assertFalse(advertised([], "h264", [1920, 1080], 60))

    def test_no_raw_copy_disguised_as_compressed_recording(self):
        with self.assertRaises(ValueError):
            codec_args(Config("ffmpeg", "ffprobe", encoder="copy"))
        self.assertIn("copy", codec_args(Config("ffmpeg", "ffprobe", encoder="copy", input_codec="h264")))

    def test_combined_device_names_are_passed_as_one_argument(self):
        c = Config("ffmpeg", "ffprobe", video="Card & camera", audio="Audio (USB)")
        args = command(c)
        self.assertIn("video=Card & camera:audio=Audio (USB)", args)
        self.assertNotIn("shell", args)

    def test_alternative_ids_and_apostrophes(self):
        self.assertEqual(device_token(r"@device_pnp_\\?\usb#123"), r"@device_pnp_\\?\usb#123")
        self.assertEqual(device_token("Josh's card"), "Josh's card")
        with self.assertRaisesRegex(ValueError, "alternative"):
            device_token("Audio: Main")

    def test_auto_fallback_uses_runtime_results(self):
        available = {e: {"works": e == "libx264", "listed": True} for e in ENCODERS}
        with patch("recording_lab.probe_encoders", return_value=available):
            self.assertEqual(choose_encoder(Config("ffmpeg", "ffprobe", encoder="auto")).encoder, "libx264")
        available["libx264"]["works"] = False
        with patch("recording_lab.probe_encoders", return_value=available):
            with self.assertRaises(ValueError):
                choose_encoder(Config("ffmpeg", "ffprobe", encoder="auto"))

    def test_video_only_does_not_require_audio(self):
        args = command(Config("ffmpeg", "ffprobe", video="Card"))
        self.assertNotIn("0:a:0", args)

    def test_mjpeg_is_requested_on_input_before_encoding(self):
        args = command(Config("ffmpeg", "ffprobe", video="Card", input_codec="mjpeg"))
        self.assertLess(args.index("-vcodec"), args.index("-i"))
        self.assertGreater(args.index("-c:v"), args.index("-i"))

    def test_preview_has_single_capture_input(self):
        args = command(Config("ffmpeg", "ffprobe", video="Card", preview=True, mpv="mpv"))
        self.assertEqual(args.count("-i"), 1)
        self.assertEqual(args[-4:], ["copy", "-f", "nut", "pipe:1"])

    def test_synthetic_preview_does_not_copy_wrapped_avframes(self):
        args = command(Config("ffmpeg", "ffprobe", synthetic=True, preview=True, mpv="mpv"))
        self.assertIn("rawvideo", args)
        self.assertIn("pcm_s16le", args)

    def test_storage_boundaries(self):
        self.assertEqual(storage_reason(99, 100, 0, 1000), "low_disk")
        self.assertEqual(storage_reason(100, 100, 1000, 1000), "size_limit")
        self.assertEqual(storage_reason(100, 100, 999, 1000), "")

    def test_low_space_refuses_to_spawn(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("recording_lab.shutil.disk_usage") as disk, patch("recording_lab.sp.Popen") as spawn:
                disk.return_value.free = 1
                with self.assertRaisesRegex(ValueError, "Insufficient"):
                    record(Config("ffmpeg", "ffprobe", synthetic=True), folder)
                spawn.assert_not_called()

    def test_runs_never_reuse_output_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            for _ in range(2):
                with patch("recording_lab.shutil.disk_usage") as disk:
                    disk.return_value.free = 0
                    with self.assertRaises(ValueError):
                        record(Config("ffmpeg", "ffprobe", synthetic=True), folder)
            self.assertEqual(len(list(Path(folder).iterdir())), 2)

    def test_container_modes(self):
        c = Config("ffmpeg", "ffprobe", synthetic=True)
        self.assertIn("+frag_keyframe+empty_moov+default_base_moof", command(dataclasses.replace(c, container="fragmented")))
        self.assertIn("record-%03d.mkv", command(dataclasses.replace(c, container="segments")))

    def test_public_summary_omits_local_paths_and_device_details(self):
        result = summarize({"case": "test", "folder": "private path", "error": "private path",
                            "config": {"video": "private name", "ffmpeg": "private path", "encoder": "libx264"},
                            "files": [{"file": "record.mkv", "decode_error": "private path", "bytes": 10}]})
        self.assertNotIn("private", str(result))


if __name__ == "__main__":
    unittest.main()
