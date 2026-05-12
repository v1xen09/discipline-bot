import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import Database

log = logging.getLogger(__name__)

_BUTTON_MAX_LEN = 28  # символов для превью в кнопке


def _note_preview(content: str) -> str:
    """Первые слова заметки, обрезанные до _BUTTON_MAX_LEN символов."""
    preview = content.strip().replace("\n", " ")
    if len(preview) > _BUTTON_MAX_LEN:
        preview = preview[:_BUTTON_MAX_LEN].rstrip() + "…"
    return preview


def _list_keyboard(notes: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Новая заметка", callback_data="note:new")],
    ]
    for n in notes:
        rows.append([InlineKeyboardButton(
            _note_preview(n["content"]),
            callback_data=f"note:view:{n['id']}",
        )])
    rows.append([InlineKeyboardButton("← Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def _detail_keyboard(note_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑 Удалить", callback_data=f"note:delete:{note_id}"),
            InlineKeyboardButton("← К заметкам", callback_data="note:list"),
        ],
    ])


async def _render_list(notes: list[dict], *, update=None, target_message=None) -> None:
    text = f"📝 <b>Заметки</b> ({len(notes)})\n\nВыбери заметку:"
    keyboard = _list_keyboard(notes)
    if target_message is not None:
        await target_message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_html(text, reply_markup=keyboard)


async def _render_detail(note: dict, *, target_message) -> None:
    date_str = (note.get("created_at") or "")[:10]
    source = " · <i>ИИ</i>" if note.get("source") == "ai" else ""
    text = (
        f"📝 <b>Заметка</b> · <i>{date_str}</i>{source}\n\n"
        f"{note['content']}"
    )
    await target_message.edit_text(
        text, parse_mode="HTML", reply_markup=_detail_keyboard(note["id"])
    )


# ── Публичный API для menu_handler ──────────────────────────────────────────

async def _render_notes(
    notes: list[dict],
    *,
    update=None,
    target_message=None,
) -> None:
    """Показать список заметок (используется из menu_handler и notes_command)."""
    await _render_list(notes, update=update, target_message=target_message)


# ── Команды ──────────────────────────────────────────────────────────────────

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

    await _render_list(notes, update=update)


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_notes_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    # Сбрасываем флаги ожидания ввода при навигации
    ctx.user_data.pop("awaiting_city", None)
    ctx.user_data.pop("awaiting_note", None)

    db: Database = ctx.bot_data["db"]
    user = query.from_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    parts = query.data.split(":")  # note:<action>[:<id>]
    if len(parts) < 2:
        return
    action = parts[1]

    # ── Список ───────────────────────────────────────────────────────────────
    if action == "list":
        notes = await db.get_notes(db_user["id"])
        if not notes:
            await query.edit_message_text(
                "📝 Заметок пока нет.\n\nДобавь через /note или напиши «запомни…»",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("← Меню", callback_data="menu:main")]
                ]),
            )
        else:
            await _render_list(notes, target_message=query.message)
        return

    # ── Просмотр карточки ─────────────────────────────────────────────────────
    if action == "view" and len(parts) >= 3:
        try:
            note_id = int(parts[2])
        except ValueError:
            return
        notes = await db.get_notes(db_user["id"])
        note = next((n for n in notes if n["id"] == note_id), None)
        if note is None:
            await query.answer("Заметка не найдена.", show_alert=True)
            return
        await _render_detail(note, target_message=query.message)
        return

    # ── Удаление ──────────────────────────────────────────────────────────────
    if action == "delete" and len(parts) >= 3:
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
            await query.edit_message_text(
                "📝 Заметок больше нет.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("← Меню", callback_data="menu:main")]
                ]),
            )
        else:
            await _render_list(notes, target_message=query.message)
        return

    # ── Новая заметка ─────────────────────────────────────────────────────────
    if action == "new":
        ctx.user_data["awaiting_note"] = True
        await query.edit_message_text(
            "📝 <b>Новая заметка</b>\n\nНапиши текст заметки в следующем сообщении:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("← К заметкам", callback_data="note:list")]
            ]),
        )
        return
