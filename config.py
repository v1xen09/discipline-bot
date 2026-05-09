"""Configuration loaded from .env."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Telegram ───────────────────────────────────────────────────────────────
    TELEGRAM_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))

    # ── LM Studio (LLM) ────────────────────────────────────────────────────────
    # URL OpenAI-совместимого сервера. В LM Studio: вкладка «Developer» → Start Server.
    LMSTUDIO_BASE_URL: str = field(
        default_factory=lambda: os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:4321/v1")
    )
    # Имя загруженной модели: Developer → раздел загруженной модели, или GET /models.
    LMSTUDIO_MODEL: str = field(default_factory=lambda: os.getenv("LMSTUDIO_MODEL", ""))
    # Локальный сервер не проверяет ключ, но openai SDK требует непустую строку.
    LMSTUDIO_API_KEY: str = field(
        default_factory=lambda: os.getenv("LMSTUDIO_API_KEY", "lm-studio")
    )
    # Сколько секунд ждать ответ модели (генерация на CPU может быть долгой).
    LMSTUDIO_TIMEOUT: int = field(
        default_factory=lambda: int(os.getenv("LMSTUDIO_TIMEOUT", "180"))
    )

    # ── faster-whisper (STT) ───────────────────────────────────────────────────
    # tiny | base | small | medium | large-v3 — small: баланс качества/скорости.
    WHISPER_MODEL_SIZE: str = field(
        default_factory=lambda: os.getenv("WHISPER_MODEL_SIZE", "small")
    )
    WHISPER_DEVICE: str = field(default_factory=lambda: os.getenv("WHISPER_DEVICE", "cpu"))
    WHISPER_COMPUTE_TYPE: str = field(
        default_factory=lambda: os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    )

    # ── Яндекс.Погода (опционально) ───────────────────────────────────────────
    # Ключ: https://developer.tech.yandex.ru/ → тестовый план (50 req/day, бесплатно)
    YANDEX_WEATHER_KEY: str = field(default_factory=lambda: os.getenv("YANDEX_WEATHER_KEY", "fa0f11a5-fd86-48c2-b07f-ef6e45a933a8"))

    # ── Storage ────────────────────────────────────────────────────────────────
    DATABASE_PATH: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "tmanager.db"))

    BOT_NAME: str = field(default_factory=lambda: os.getenv("BOT_NAME", "TManager"))

    def validate(self) -> None:
        if not self.TELEGRAM_TOKEN:
            raise ValueError("TELEGRAM_TOKEN is not set")
        if not self.LMSTUDIO_BASE_URL:
            raise ValueError("LMSTUDIO_BASE_URL is not set")
        if not self.LMSTUDIO_MODEL:
            raise ValueError(
                "LMSTUDIO_MODEL is not set — укажи в .env имя модели, "
                "загруженной в LM Studio (например, 'qwen/qwen3-8b')"
            )
