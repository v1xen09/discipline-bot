"""Configuration loaded from .env."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    TELEGRAM_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))

    LMSTUDIO_BASE_URL: str = field(
        default_factory=lambda: os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:4321/v1")
    )
    LMSTUDIO_MODEL: str = field(default_factory=lambda: os.getenv("LMSTUDIO_MODEL", ""))
    # openai SDK требует непустую строку; LM Studio ключ не проверяет.
    LMSTUDIO_API_KEY: str = field(
        default_factory=lambda: os.getenv("LMSTUDIO_API_KEY", "lm-studio")
    )
    LMSTUDIO_TIMEOUT: int = field(
        default_factory=lambda: int(os.getenv("LMSTUDIO_TIMEOUT", "180"))
    )

    WHISPER_MODEL_SIZE: str = field(
        default_factory=lambda: os.getenv("WHISPER_MODEL_SIZE", "small")
    )
    WHISPER_DEVICE: str = field(default_factory=lambda: os.getenv("WHISPER_DEVICE", "cpu"))
    WHISPER_COMPUTE_TYPE: str = field(
        default_factory=lambda: os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    )

    # Ключ Яндекс.Погоды: https://developer.tech.yandex.ru/ → тестовый план (50 req/day).
    YANDEX_WEATHER_KEY: str = field(default_factory=lambda: os.getenv("YANDEX_WEATHER_KEY", "fa0f11a5-fd86-48c2-b07f-ef6e45a933a8"))

    DATABASE_PATH: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "tmanager.db"))
    BOT_NAME: str = field(default_factory=lambda: os.getenv("BOT_NAME", "TManager"))

    # Telegram ID владельца — открывает /admin. 0 = отключено.
    ADMIN_TELEGRAM_ID: int = field(
        default_factory=lambda: int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
    )

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
