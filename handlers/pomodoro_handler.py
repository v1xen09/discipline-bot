import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

DEFAULT_WORK_MIN = 25
DEFAULT_SHORT_BREAK = 5
DEFAULT_LONG_BREAK = 15


async def pomodoro_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []

    if args and args[0].lower() == "stop":
        task: asyncio.Task | None = ctx.user_data.get("pomodoro_task")
        if task and not task.done():
            task.cancel()
            ctx.user_data.pop("pomodoro_task", None)
            await update.message.reply_text("⏹ Таймер остановлен.")
        else:
            await update.message.reply_text("Нет активного таймера.")
        return

    task = ctx.user_data.get("pomodoro_task")
    if task and not task.done():
        remaining = ctx.user_data.get("pomodoro_end_ts", 0) - asyncio.get_event_loop().time()
        mins_left = max(0, int(remaining / 60))
        await update.message.reply_text(
            f"⏱ Таймер уже запущен (осталось ~{mins_left} мин).\n"
            "Используй /pomodoro stop чтобы остановить."
        )
        return

    minutes = DEFAULT_WORK_MIN
    if args:
        try:
            minutes = max(1, min(120, int(args[0])))
        except ValueError:
            await update.message.reply_text(
                "Укажи число минут: /pomodoro 45\nИли запусти по умолчанию: /pomodoro"
            )
            return

    ctx.user_data["pomodoro_end_ts"] = asyncio.get_event_loop().time() + minutes * 60
    ctx.user_data["pomodoro_task"] = asyncio.create_task(
        _work_timer(ctx.application, update.effective_user.id, minutes)
    )

    await update.message.reply_html(
        f"🍅 <b>Помодоро запущен — {minutes} мин</b>\n\n"
        f"Убери телефон, закрой лишние вкладки. Фокус!"
    )


async def handle_pomodoro_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) < 3:
        return
    action = parts[1]
    try:
        minutes = int(parts[2])
    except ValueError:
        return

    user_id = query.from_user.id

    if action == "work":
        await query.edit_message_text(
            f"🍅 <b>Помодоро — {minutes} мин</b>. Фокус!",
            parse_mode="HTML",
        )
        ctx.user_data["pomodoro_end_ts"] = asyncio.get_event_loop().time() + minutes * 60
        ctx.user_data["pomodoro_task"] = asyncio.create_task(
            _work_timer(ctx.application, user_id, minutes)
        )

    elif action == "break":
        label = "☕ Короткий перерыв" if minutes <= 5 else "🛋 Длинный перерыв"
        await query.edit_message_text(
            f"{label} — <b>{minutes} мин</b>. Отдыхай!",
            parse_mode="HTML",
        )
        ctx.user_data["pomodoro_end_ts"] = asyncio.get_event_loop().time() + minutes * 60
        ctx.user_data["pomodoro_task"] = asyncio.create_task(
            _break_timer(ctx.application, user_id, minutes)
        )


async def _work_timer(app, user_id: int, minutes: int) -> None:
    try:
        await asyncio.sleep(minutes * 60)
    except asyncio.CancelledError:
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("☕ Перерыв 5 мин", callback_data=f"pomodoro:break:{DEFAULT_SHORT_BREAK}"),
            InlineKeyboardButton("🛋 Перерыв 15 мин", callback_data=f"pomodoro:break:{DEFAULT_LONG_BREAK}"),
        ],
        [InlineKeyboardButton(f"🍅 Ещё {minutes} мин", callback_data=f"pomodoro:work:{minutes}")],
    ])
    try:
        await app.bot.send_message(
            user_id,
            f"✅ <b>{minutes} мин сосредоточенной работы — готово!</b>\n\n"
            "Сделай перерыв или сразу следующий раунд?",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        log.warning("Pomodoro work notify failed for %d: %s", user_id, e)


async def _break_timer(app, user_id: int, minutes: int) -> None:
    try:
        await asyncio.sleep(minutes * 60)
    except asyncio.CancelledError:
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🍅 Помодоро {DEFAULT_WORK_MIN} мин", callback_data=f"pomodoro:work:{DEFAULT_WORK_MIN}"),
    ]])
    try:
        await app.bot.send_message(
            user_id,
            "⏰ <b>Перерыв окончен!</b> Готов к следующему раунду?",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        log.warning("Pomodoro break notify failed for %d: %s", user_id, e)
