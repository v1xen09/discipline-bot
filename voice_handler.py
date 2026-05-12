import asyncio
import logging
import os
import tempfile

from config import Config

log = logging.getLogger(__name__)


class WhisperVoiceHandler:
    def __init__(self, config: Config) -> None:
        self.model_size = config.WHISPER_MODEL_SIZE
        self.device = config.WHISPER_DEVICE
        self.compute_type = config.WHISPER_COMPUTE_TYPE
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            log.info(
                "Loading faster-whisper model: size=%s device=%s compute_type=%s",
                self.model_size, self.device, self.compute_type,
            )
            self._model = WhisperModel(
                self.model_size, device=self.device, compute_type=self.compute_type,
            )
            log.info("faster-whisper model loaded")
        return self._model

    def _transcribe_sync(self, audio_path: str) -> str:
        model = self._ensure_model()
        segments, info = model.transcribe(
            audio_path, language="ru", beam_size=5, vad_filter=True,
        )
        log.debug("Whisper detected language=%s prob=%.2f", info.language, info.language_probability)
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, file_bytes: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            return await asyncio.to_thread(self._transcribe_sync, tmp_path)
        except Exception as e:
            log.exception("Whisper transcription failed: %s", e)
            raise
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
