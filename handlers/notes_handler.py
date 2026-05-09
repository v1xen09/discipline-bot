"""
/note <text>  — добавить заметку
/notes        — список заметок с кнопками удаления

ИИ тоже может сохранять заметки через intent=add_note (см. ai_chat_handler).
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import Database

log = logging.getLogger(__name__)


async def note_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    user = update.effective_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text(
            "Как добавить заметку:\n"
            "/note Текст заметки\n\n"
            "Или просто напиши боту: «запомни, что…» / «сохрани идею…»\n"
            "Список заметок: /notes"
        )
        return

    await db.add_note(db_user["id"], text)
    await update.message.reply_html(f"📝 Сохранено\n\n<i>{text}</i>")


async def notes_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    user = update.effective_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    notes = await db.get_notes(db_user["id"])
    if not notes:
        await update.message.reply_text(
            "Заметок пока нет.\n\n"
            "Добавь через /note или напиши боту «запомни / сохрани…»"
        )
        return

    await _render_notes(notes, update=update)


async def handle_notes_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    db: Database = ctx.bot_data["db"]
    user = query.from_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    parts = query.data.split(":")
    if len(parts) < 3 or parts[1] != "delete":
        return

    try:
        note_id = int(parts[2])
    except ValueError:
        return

    deleted = await db.delete_note(note_id, db_user["id"])
    if not deleted:
        await query.answer("Заметка не найдена.", show_alert=True)
        return

    notes = await db.get_notes(db_user["id"])
    if not notes:
        await query.edit_message_text("📝 Заметок больше нет.")
    else:
        await _render_notes(notes, target_message=query.message)


def _build_notes_text(notes: list[dict]) -> str:
    lines = [f"📝 <b>Заметки</b> ({len(notes)})\n"]
    for n in notes:
        date_str = (n.get("created_at") or "")[:10]
        source_mark = " <i>(ИИ)</i>" if n.get("source") == "ai" else ""
        lines.append(f"<b>{n['id']}.</b> <i>{date_str}</i>{source_mark}\n{n['content']}")
    return "\n\n".join(lines)


def _build_notes_keyboard(notes: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n in notes:
        row.append(InlineKeyboardButton(f"🗑 {n['id']}", callback_data=f"note:delete:{n['id']}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def _render_notes(
    notes: list[dict],
    *,
    update=None,
    target_message=None,
) -> None:
    text = _build_notes_text(notes)
    keyboard = _build_notes_keyboard(notes)
    if target_message is not None:
        await target_message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_html(text, reply_markup=keyboard)
