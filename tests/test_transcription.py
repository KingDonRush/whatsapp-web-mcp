from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from whatsapp_web_mcp.transcription import python_module_available, transcribe_file


class TranscriptionContractTests(unittest.TestCase):
    def test_transcribe_file_defaults_to_whisperx(self) -> None:
        signature = inspect.signature(transcribe_file)

        self.assertEqual(signature.parameters["backend"].default, "whisperx")

    def test_transcribe_file_rejects_vibe_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.wav"
            audio.write_bytes(b"fixture")

            with self.assertRaisesRegex(ValueError, "WhisperX"):
                transcribe_file(str(audio), backend="vibe", prepare=False)

    def test_transcribe_file_uses_whisperx_when_backend_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.wav"
            audio.write_bytes(b"fixture")
            with patch(
                "whatsapp_web_mcp.transcription.transcribe_with_whisperx",
                return_value={"backend": "whisperx", "text": "ok"},
            ) as whisperx:
                result = transcribe_file(str(audio), prepare=False)

        self.assertEqual(result["backend"], "whisperx")
        self.assertEqual(result["original_file"], str(audio))
        whisperx.assert_called_once()

    def test_python_module_available_checks_the_configured_interpreter(self) -> None:
        self.assertTrue(python_module_available(Path(sys.executable), "json"))
        self.assertFalse(
            python_module_available(Path(sys.executable), "module_that_does_not_exist_12345")
        )


if __name__ == "__main__":
    unittest.main()
