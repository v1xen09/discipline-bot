"""
TManager — main entry point.

Локальный стек, ничего не уходит в облако:
  • python-telegram-bot v21  — асинхронный клиент Telegram
  • LM Studio (через openai SDK) — локальная LLM для расписания, мотивации и чата
  • faster-whisper — локальное распознавание голоса
  • aiosqlite — асинхронный SQLite
  • APScheduler — фоновые задания (утро / вечер / напоминания)

Замечание про event loop:
  python-telegram-bot v21 сам управляет своим asyncio loop в run_polling().
  Поэтому main() здесь СИНХРОННАЯ — никакого asyncio.run(main()).
  Всё, что должно быть async (db.init, scheduler.start), уносится в
  post_init-хук, который PTB вызывает внутри своего loop.
"""

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ai_client import AIClient
from config import Config
from database import Database
from handlers.ai_chat_handler import handle_text_message
from handlers.schedule_handler import (
    AWAITING_SCHEDULE_WEEK,
    AWAITING_SCHEDULE_REQUEST,
    handle_schedule_callback,
    myplan_command,
    receive_schedule_request,
    schedule_command,
    schedule_week_selected,
)
from handlers.analytics_handler import month_command, today_command, week_command
from handlers.notes_handler import handle_notes_callback, note_command, notes_command
from handlers.pomodoro_handler import handle_pomodoro_callback, pomodoro_command
from handlers.settings_handler import handle_settings_callback, settings_command
from handlers.start_handler import help_command, start_command
from handlers.task_handler import (
    done_command,
    handle_task_callback,
    overdue_command,
    task_command,
    tasks_command,
)
from handlers.admin_handler import admin_command, handle_admin_callback
from handlers.location_handler import handle_location
from handlers.menu_handler import handle_menu_callback, menu_command
from handlers.weather_handler import weather_command
from handlers.voice_message_handler import handle_voice
from scheduler_jobs import setup_scheduler
from voice_handler import WhisperVoiceHandler
from weather_client import WeatherClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Подробные логи только от нашего ai_client'а — увидим длину/содержимое
# ответов модели, не утопая в шуме httpx и telegram.
logging.getLogger("ai_client").setLevel(logging.DEBUG)
log = logging.getLogger(__name__)


async def _post_init(app: Application) -> None:
    """Запускается уже внутри event loop'а PTB — здесь делаем всё async."""
    db: Database = app.bot_data["db"]
    await db.init()

    await app.bot.set_my_commands([
        BotCommand("menu",     "Главное меню"),
        BotCommand("help",     "Справка по всем командам"),
        BotCommand("task",     "Добавить задачу: /task Название | дата"),
        BotCommand("tasks",    "Список активных задач"),
        BotCommand("done",     "Отметить задачу выполненной: /done ID"),
        BotCommand("overdue",  "Просроченные задачи"),
        BotCommand("myplan",   "Расписание на неделю"),
        BotCommand("schedule", "Составить новое расписание с помощью ИИ"),
        BotCommand("note",     "Добавить заметку: /note Текст"),
        BotCommand("notes",    "Список всех заметок"),
        BotCommand("pomodoro", "Таймер помодоро: /pomodoro [мин] или stop"),
        BotCommand("today",    "Статистика продуктивности за сегодня"),
        BotCommand("week",     "Статистика за текущую неделю"),
        BotCommand("month",    "Статистика за текущий месяц"),
        BotCommand("weather",  "Текущая погода"),
        BotCommand("settings", "Настройки"),
    ])

    scheduler = setup_scheduler(app)
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    log.info("Scheduler started")

    import threading
    from api import flask_app
    api_thread = threading.Thread(
        target=flask_app.run,
        kwargs={"host": "0.0.0.0", "port": 8080, "threaded": True, "use_reloader": False},
        daemon=True,
    )
    api_thread.start()
    log.info("API server started on :8080")

    log.info("TManager ready (model=%s, stt=%s)",
             app.bot_data["config"].LMSTUDIO_MODEL,
             app.bot_data["config"].WHISPER_MODEL_SIZE)


async def _post_shutdown(app: Application) -> None:
    """Аккуратно останавливаем scheduler при Ctrl+C."""
    scheduler = app.bot_data.get("scheduler")
    if scheduler is not None:
        scheduler.shutdown(wait=False)


async def _error_handler(update: object, ctx) -> None:
    """
    Глобальный обработчик ошибок. PTB вызывает его, когда внутри handler'а
    выскочило исключение. Здесь мы:
      • записываем стек в лог (как и раньше),
      • пытаемся сказать пользователю что-то осмысленное вместо тишины.

    Особо ловим сетевые ошибки (TimedOut/NetworkError) — они почти всегда
    означают, что VPN/прокси временно тупит и ничего страшного не случилось.
    """
    from telegram.error import NetworkError, TimedOut

    err = ctx.error
    log.error("Handler raised an exception", exc_info=err)

    # Попытаемся ответить пользователю, если это его сообщение
    try:
        from telegram import Update as _Update
        if isinstance(update, _Update) and update.effective_message is not None:
            if isinstance(err, (TimedOut, NetworkError)):
                msg = "🌐 Соединение с Telegram сейчас флапает (скорее всего, VPN). Попробуй ещё раз через минуту."
            else:
                msg = f"⚠️ Что-то пошло не так: {type(err).__name__}. Попробуй ещё раз."
            await update.effective_message.reply_text(msg)
    except Exception as e:
        log.warning("Failed to notify user about error: %s", e)


def _with_user_lock(handler):
    """Гарантирует, что один пользователь не может запустить handler дважды одновременно."""
    async def wrapper(update: Update, ctx):
        user_id = update.effective_user.id
        active: set = ctx.bot_data.setdefault("active_users", set())
        if user_id in active:
            await update.effective_message.reply_text(
                "⏳ Подожди — я ещё обрабатываю твоё предыдущее сообщение."
            )
            return
        active.add(user_id)
        try:
            await handler(update, ctx)
        finally:
            active.discard(user_id)
    return wrapper


def main() -> None:
    # Python 3.14: asyncio.get_event_loop() больше не создаёт loop сам, если
    # в потоке его нет — теперь он бросает RuntimeError. python-telegram-bot
    # 21.6 рассчитывает на старое поведение внутри run_polling(). Поэтому
    # создаём loop вручную и кладём его в текущий поток.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    config = Config()
    config.validate()

    # Сами объекты создаём синхронно — async-инициализацию (db.init)
    # делает _post_init уже внутри loop'а PTB.
    db = Database(config.DATABASE_PATH)
    ai = AIClient(config)
    voice = WhisperVoiceHandler(config)
    weather = WeatherClient(config.YANDEX_WEATHER_KEY)

    # HTTP-таймауты PTB по умолчанию 5 секунд. Для VPN/прокси этого мало —
    # TLS-рукопожатие через медленный туннель может занять и 10–20 секунд.
    # Поднимаем все четыре таймаута до 30 секунд, чтобы случайный лаг VPN
    # не превращался в TimedOut и не валил handler'ы.
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Inject shared objects into bot_data
    app.bot_data["config"] = config
    app.bot_data["db"] = db
    app.bot_data["ai"] = ai
    app.bot_data["voice"] = voice
    app.bot_data["weather"] = weather

    # ── Conversation: schedule creation ───────────────────────────────────────
    schedule_conv = ConversationHandler(
        entry_points=[CommandHandler("schedule", schedule_command)],
        states={
            AWAITING_SCHEDULE_WEEK: [
                CallbackQueryHandler(schedule_week_selected, pattern=r"^schedule_week_select:"),
            ],
            AWAITING_SCHEDULE_REQUEST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule_request)
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    # ── Command handlers ───────────────────────────────────────────────────────
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("task", task_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("overdue", overdue_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("month", month_command))
    app.add_handler(CommandHandler("myplan", myplan_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(CommandHandler("notes", notes_command))
    app.add_handler(CommandHandler("pomodoro", pomodoro_command))
    app.add_handler(schedule_conv)

    # ── Voice messages & location ──────────────────────────────────────────────
    app.add_handler(MessageHandler(filters.VOICE, _with_user_lock(handle_voice)))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    # ── Inline keyboard callbacks ──────────────────────────────────────────────
    # menu: ПЕРВЫМ — до tasks?:, т.к. menu:* не попадает под tasks?:, но для надёжности
    app.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(handle_schedule_callback, pattern=r"^(schedule_|myplan:)"))
    # task:done|delete:<id>[:<page>] — действия по задаче (одиночная карточка или из списка)
    # tasks:page:<n> / tasks:noop   — перелистывание постраничного /tasks
    app.add_handler(CallbackQueryHandler(handle_task_callback, pattern=r"^tasks?:"))
    # settings:* — переключение характера, очистка истории
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^settings:"))
    # note:delete:<id>
    app.add_handler(CallbackQueryHandler(handle_notes_callback, pattern=r"^note:"))
    # pomodoro:work|break:<minutes>
    app.add_handler(CallbackQueryHandler(handle_pomodoro_callback, pattern=r"^pomodoro:"))

    # ── Free-form text (AI chat) ───────────────────────────────────────────────
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _with_user_lock(handle_text_message)))

    # ── Global error handler ──────────────────────────────────────────────────
    app.add_error_handler(_error_handler)

    log.info("TManager starting…")
    # run_polling() сам создаёт и закрывает event loop. Блокирующий вызов.
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
