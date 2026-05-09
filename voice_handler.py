"""
Speech-to-text провайдеры.

Выбор провайдера определяется config.SPEECH_PROVIDER (задаётся автоматически
из DEPLOYMENT_MODE или вручную):
  • whisper  — faster-whisper, работает локально, модель кешируется в HF Hub
  • yandex   — Yandex SpeechKit REST API, требует YANDEX_API_KEY
"""

import asyncio
import logging
import os
import tempfile
from typing import Optional

from config import Config

log = logging.getLogger(__name__)


# ─── Whisper (локальный) ───────────────────────────────────────────────────────

class WhisperVoiceHandler:
    """Распознавание через faster-whisper. Модель грузится лениво при первом вызове."""

    def __init__(self, config: Config) -> None:
        self.model_size = config.WHISPER_MODEL_SIZE
        self.device = config.WHISPER_DEVICE
        self.compute_type = config.WHISPER_COMPUTE_TYPE
        self._model = None  # lazy load

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


# ─── Yandex SpeechKit (облачный) ──────────────────────────────────────────────

class YandexSpeechKitHandler:
    """Распознавание через Yandex SpeechKit REST API v1."""

    _URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"

    def __init__(self, config: Config) -> None:
        self._api_key = config.YANDEX_API_KEY

    async def transcribe(self, file_bytes: bytes) -> str:
        import httpx
        params = {"lang": "ru-RU", "topic": "general", "profanityFilter": "false"}
        headers = {"Authorization": f"Api-Key {self._api_key}"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self._URL, content=file_bytes, params=params, headers=headers,
            )
            resp.raise_for_status()
            result = resp.json().get("result", "")
            log.debug("SpeechKit result: %s", result)
            return result


# ─── Фабрика ──────────────────────────────────────────────────────────────────

def create_voice_handler(config: Config):
    """Вернуть нужный обработчик STT на основе config.SPEECH_PROVIDER."""
    if config.SPEECH_PROVIDER == "yandex":
        log.info("STT provider: Yandex SpeechKit")
        return YandexSpeechKitHandler(config)
    log.info("STT provider: faster-whisper (%s)", config.WHISPER_MODEL_SIZE)
    return WhisperVoiceHandler(config)
