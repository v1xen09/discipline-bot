import logging
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import Database

log = logging.getLogger(__name__)

DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Мой план", callback_data="menu:plan"),
            InlineKeyboardButton("📋 Задачи", callback_data="menu:tasks"),
        ],
        [
            InlineKeyboardButton("📝 Заметки",  callback_data="menu:notes"),
            InlineKeyboardButton("📊 Сегодня",  callback_data="menu:today"),
        ],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")],
    ])


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Меню", callback_data="menu:main")]
    ])


async def _render_main_menu(query) -> None:
    text = (
        "📋 <b>TManager</b>\n\n"
        "Выбери раздел:"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=_main_keyboard())


async def menu_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📋 <b>TManager</b>\n\n"
        "Выбери раздел:"
    )
    await update.message.reply_html(text, reply_markup=_main_keyboard())


async def handle_menu_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    ctx.user_data.pop("awaiting_city", None)
    ctx.user_data.pop("awaiting_note", None)
    ctx.user_data.pop("awaiting_schedule_edit", None)

    db: Database = ctx.bot_data["db"]
    user = query.from_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    action = query.data.split(":", 1)[1]

    if action == "main":
        await _render_main_menu(query)
        return

    if action == "tasks":
        from handlers.task_handler import _send_or_edit_tasks_page
        await _send_or_edit_tasks_page(
            db, db_user["id"], page=0, target_message=query.message
        )
        return

    if action == "plan":
        from datetime import timedelta
        from handlers.schedule_handler import _render_week_plan
        monday = date.today() - timedelta(days=date.today().weekday())
        grouped = await db.get_week_tasks_grouped(db_user["id"], monday)
        today_day = DAY_KEYS[date.today().weekday()]
        await _render_week_plan(
            grouped, monday, target_message=query.message, day_key=today_day
        )
        return

    if action == "notes":
        notes = await db.get_notes(db_user["id"])
        if not notes:
            await query.edit_message_text(
                "Заметок пока нет.\n\nДобавь через /note или напиши «запомни…»",
                reply_markup=_back_keyboard(),
            )
            return
        from handlers.notes_handler import _render_notes
        await _render_notes(notes, target_message=query.message)
        return

    if action == "today":
        stat = await db.daily_stats(db_user["id"], date.today())
        if stat["rate"] is None:
            rate_str = "нет данных"
        else:
            rate_str = f"{int(round(stat['rate'] * 100))}%"
        text = (
            f"📊 <b>Сегодня, {date.today().strftime('%d.%m')}</b>\n\n"
            f"Выполнено: <b>{stat['completed']} из {stat['planned']}</b>\n"
            f"Продуктивность: <b>{rate_str}</b>\n\n"
            "<i>Для графика используй /today</i>"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_back_keyboard())
        return

    if action == "settings":
        from handlers.settings_handler import _render_settings
        text, keyboard = await _render_settings(db, user.id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return
