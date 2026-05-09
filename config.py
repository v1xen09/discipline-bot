"""
Configuration loaded from .env.

Режим развёртывания (DEPLOYMENT_MODE):
  • local  — LM Studio (LLM) + faster-whisper (STT), всё работает локально
  • cloud  — YandexGPT (LLM) + Yandex SpeechKit (STT), облачная инфраструктура

AI_PROVIDER и SPEECH_PROVIDER устанавливаются автоматически из DEPLOYMENT_MODE.
Можно переопределить вручную для смешанных конфигураций.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Telegram ───────────────────────────────────────────────────────────────
    TELEGRAM_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))

    # ── LM Studio (LLM) ────────────────────────────────────────────────────────
    # URL OpenAI-совместимого сервера LM Studio. По умолчанию это
    # http://localhost:1234/v1 — открой в LM Studio вкладку «Developer» и
    # включи «Start Server», чтобы он начал слушать.
    LMSTUDIO_BASE_URL: str = field(
        default_factory=lambda: os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:4321/v1")
    )
    # Имя модели, которую LM Studio загрузил в память. Можно посмотреть
    # на вкладке «Developer» → раздел загруженной модели, или вызвать
    # GET <LMSTUDIO_BASE_URL>/models.
    LMSTUDIO_MODEL: str = field(default_factory=lambda: os.getenv("LMSTUDIO_MODEL", ""))
    # Локальный сервер не проверяет ключ, но openai SDK требует, чтобы
    # api_key был непустой строкой.
    LMSTUDIO_API_KEY: str = field(
        default_factory=lambda: os.getenv("LMSTUDIO_API_KEY", "lm-studio")
    )
    # Сколько секунд ждать ответ от модели (генерация на CPU может быть долгой).
    LMSTUDIO_TIMEOUT: int = field(
        default_factory=lambda: int(os.getenv("LMSTUDIO_TIMEOUT", "180"))
    )

    # ── Режим развёртывания ────────────────────────────────────────────────────
    # 'local' (по умолчанию) или 'cloud'
    DEPLOYMENT_MODE: str = field(default_factory=lambda: os.getenv("DEPLOYMENT_MODE", "local"))

    # ── AI-провайдер ──────────────────────────────────────────────────────────
    # Устанавливается автоматически из DEPLOYMENT_MODE, или вручную.
    # 'lmstudio' или 'yandex'
    AI_PROVIDER: str = field(default_factory=lambda: os.getenv("AI_PROVIDER", ""))

    # ── Яндекс AI (нужно только при AI_PROVIDER=yandex) ───────────────────────
    YANDEX_API_KEY: str = field(default_factory=lambda: os.getenv("YANDEX_API_KEY", ""))
    YANDEX_FOLDER_ID: str = field(default_factory=lambda: os.getenv("YANDEX_FOLDER_ID", ""))
    # yandexgpt (умнее) или yandexgpt-lite (быстрее/дешевле)
    YANDEX_MODEL: str = field(default_factory=lambda: os.getenv("YANDEX_MODEL", "yandexgpt"))

    # ── Speech-to-text провайдер ──────────────────────────────────────────────
    # Устанавливается автоматически из DEPLOYMENT_MODE, или вручную.
    # 'whisper' или 'yandex'
    SPEECH_PROVIDER: str = field(default_factory=lambda: os.getenv("SPEECH_PROVIDER", ""))

    # ── faster-whisper (STT, только при SPEECH_PROVIDER=whisper) ──────────────
    # tiny | base | small | medium | large-v3
    # small — хороший баланс качества и скорости на CPU.
    WHISPER_MODEL_SIZE: str = field(
        default_factory=lambda: os.getenv("WHISPER_MODEL_SIZE", "small")
    )
    # cpu | cuda
    WHISPER_DEVICE: str = field(default_factory=lambda: os.getenv("WHISPER_DEVICE", "cpu"))
    # int8 — быстро на CPU; float16 — быстро на GPU.
    WHISPER_COMPUTE_TYPE: str = field(
        default_factory=lambda: os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    )

    # ── Storage ────────────────────────────────────────────────────────────────
    DATABASE_PATH: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "tmanager.db"))

    # ── Personality / scheduling ──────────────────────────────────────────────
    BOT_NAME: str = field(default_factory=lambda: os.getenv("BOT_NAME", "TManager"))
    MORNING_MESSAGE_TIME: str = field(
        default_factory=lambda: os.getenv("MORNING_MESSAGE_TIME", "08:00")
    )
    EVENING_REVIEW_TIME: str = field(
        default_factory=lambda: os.getenv("EVENING_REVIEW_TIME", "21:00")
    )
    REMINDER_TIME_1: str = field(
        default_factory=lambda: os.getenv("REMINDER_TIME_1", "12:00")
    )
    REMINDER_TIME_2: str = field(
        default_factory=lambda: os.getenv("REMINDER_TIME_2", "17:00")
    )

    def validate(self) -> None:
        # Установить дефолты провайдеров из DEPLOYMENT_MODE
        if self.DEPLOYMENT_MODE == "cloud":
            if not self.AI_PROVIDER:
                self.AI_PROVIDER = "yandex"
            if not self.SPEECH_PROVIDER:
                self.SPEECH_PROVIDER = "yandex"
        else:  # local
            if not self.AI_PROVIDER:
                self.AI_PROVIDER = "lmstudio"
            if not self.SPEECH_PROVIDER:
                self.SPEECH_PROVIDER = "whisper"

        if not self.TELEGRAM_TOKEN:
            raise ValueError("TELEGRAM_TOKEN is not set")

        if self.AI_PROVIDER == "lmstudio":
            if not self.LMSTUDIO_BASE_URL:
                raise ValueError("LMSTUDIO_BASE_URL is not set")
            if not self.LMSTUDIO_MODEL:
                raise ValueError(
                    "LMSTUDIO_MODEL is not set — укажи в .env имя модели, "
                    "загруженной в LM Studio (например, 'qwen/qwen3-8b')"
                )
        elif self.AI_PROVIDER == "yandex":
            if not self.YANDEX_API_KEY:
                raise ValueError(
                    "YANDEX_API_KEY is not set — укажи API-ключ сервисного "
                    "аккаунта Яндекс.Облако (роль ai.languageModels.user)"
                )
            if not self.YANDEX_FOLDER_ID:
                raise ValueError(
                    "YANDEX_FOLDER_ID is not set — укажи ID каталога "
                    "из консоли Яндекс.Облако"
                )
        else:
            raise ValueError(
                f"Неизвестный AI_PROVIDER: '{self.AI_PROVIDER}'. "
                "Допустимые значения: 'lmstudio' или 'yandex'"
            )

        if self.SPEECH_PROVIDER == "yandex" and not self.YANDEX_API_KEY:
            raise ValueError(
                "YANDEX_API_KEY is not set — нужен для SPEECH_PROVIDER=yandex "
                "(роль ai.speechkit.user)"
            )
        if self.SPEECH_PROVIDER not in ("whisper", "yandex"):
            raise ValueError(
                f"Неизвестный SPEECH_PROVIDER: '{self.SPEECH_PROVIDER}'. "
                "Допустимые значения: 'whisper' или 'yandex'"
            )
