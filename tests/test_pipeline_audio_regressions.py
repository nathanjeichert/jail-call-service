import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.audio_converter import convert_single
from backend.models import AUDIO_EXTENSIONS, CallResult, CallStatus, call_stem
from backend.pipeline import _discover_audio_files, _record_pdf_failure


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _zero_header_wav_bytes() -> bytes:
    return bytes(60) + b"\x01\x02\x03\x04" * 64


class PipelineAudioRegressionTests(unittest.TestCase):
    def test_convert_single_uses_working_copy_and_keeps_source_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_path = os.path.join(tmpdir, "evidence.wav")
            output_dir = os.path.join(tmpdir, "audio")
            working_dir = os.path.join(tmpdir, "source-working")
            os.makedirs(output_dir, exist_ok=True)

            Path(src_path).write_bytes(_zero_header_wav_bytes())
            before_hash = _sha256(src_path)
            before_mtime = os.path.getmtime(src_path)
            ffmpeg_inputs = []

            def fake_run(cmd, capture_output, text, timeout):
                ffmpeg_inputs.append(cmd[cmd.index("-i") + 1])
                Path(cmd[-1]).write_bytes(b"fake mp3")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch("backend.audio_converter.FFMPEG_PATH", "/usr/bin/ffmpeg"), \
                 patch("backend.audio_converter.get_duration", return_value=12.34), \
                 patch("backend.audio_converter.subprocess.run", side_effect=fake_run):
                result = convert_single(
                    0,
                    src_path,
                    output_dir,
                    stem="001-evidence",
                    working_dir=working_dir,
                )

            self.assertTrue(result.success)
            self.assertTrue(result.repaired)
            self.assertEqual(before_hash, _sha256(src_path))
            self.assertEqual(before_mtime, os.path.getmtime(src_path))
            self.assertEqual(
                ffmpeg_inputs,
                [os.path.join(working_dir, "001-evidence.wav")],
            )
            self.assertEqual(result.working_path, os.path.join(working_dir, "001-evidence.wav"))
            self.assertNotEqual(Path(result.working_path).read_bytes(), Path(src_path).read_bytes())
            self.assertTrue(Path(result.mp3_path).is_file())

    def test_discover_audio_files_finds_all_supported_extensions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a.wav").write_bytes(b"wav")
            (root / "b.MP3").write_bytes(b"mp3")
            nested = root / "nested"
            nested.mkdir()
            (nested / "c.m4a").write_bytes(b"m4a")
            (nested / "ignore.txt").write_bytes(b"text")

            discovered = _discover_audio_files(tmpdir)

            self.assertEqual(
                [Path(path).name for path in discovered],
                ["a.wav", "b.MP3", "c.m4a"],
            )

    def test_call_stem_strips_supported_audio_extensions(self):
        self.assertEqual(call_stem(0, "a.wav"), "001-a")
        self.assertEqual(call_stem(0, "a.mp3"), "001-a")
        self.assertEqual(call_stem(0, "a.m4a"), "001-a")
        self.assertEqual(call_stem(1, "weird name.MP3"), "002-weird_name")
        self.assertIn(".mp3", AUDIO_EXTENSIONS)

    def test_record_pdf_failure_sets_error_status_and_message(self):
        call = CallResult(
            index=3,
            filename="bad.pdf.wav",
            original_path="/tmp/bad.pdf.wav",
            status=CallStatus.GENERATING_PDF,
        )

        with patch("backend.pipeline.job_store.update_call") as update_call:
            _record_pdf_failure("job-123", call, RuntimeError("boom"))

        update_call.assert_called_once_with(
            "job-123",
            3,
            status=CallStatus.ERROR,
            error="PDF failed: boom",
        )
        self.assertEqual(call.status, CallStatus.ERROR)
        self.assertEqual(call.error, "PDF failed: boom")


if __name__ == "__main__":
    unittest.main()
