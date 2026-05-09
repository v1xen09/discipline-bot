"""
Task management commands:
  /task <title> [| YYYY-MM-DD]  — add task
  /tasks                         — list active tasks (с инлайн-кнопками)
  /done <id>                     — mark task complete (legacy, fallback)
  /overdue                       — list overdue tasks
  /streak                        — show streaks

Управление задачами в основном идёт через inline-кнопки под каждой карточкой:
  ✅ Выполнить   — отмечает задачу выполненной
  🗑 Удалить     — удаляет задачу из списка

Кнопки добавляются: (а) под каждой задачей в /tasks, (б) под подтверждением
после /task. callback_data в формате "task:done:<id>" / "task:delete:<id>".
"""

import logging
import re
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ai_client import AIClient
from database import Database

log = logging.getLogger(__name__)

STREAK_EMOJIS = {1: "🌱", 3: "🌿", 7: "🔥", 14: "⚡", 30: "💎", 100: "👑"}

PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}
_PRIORITY_RU = {
    "высокий": "high", "высокая": "high", "срочно": "high", "срочный": "high",
    "средний": "medium", "средняя": "medium",
    "низкий": "low", "низкая": "low",
}


def _streak_emoji(n: int) -> str:
    for threshold in sorted(STREAK_EMOJIS.keys(), reverse=True):
        if n >= threshold:
            return STREAK_EMOJIS[threshold]
    return "🌱"


PAGE_SIZE = 5


def _task_keyboard(task_id: int, has_time: bool = False) -> InlineKeyboardMarkup:
    """Кнопки одиночной карточки: ✅ Выполнить | … Ещё."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Выполнить", callback_data=f"task:done:{task_id}"),
        InlineKeyboardButton("… Ещё",       callback_data=f"task:menu:{task_id}"),
    ]])


_REMIND_LABELS = {
    10: "10 мин", 15: "15 мин", 30: "30 мин",
    60: "1 час",  90: "90 мин", 120: "2 часа",
}


def _task_submenu_keyboard(task: dict, page) -> InlineKeyboardMarkup:
    """Подменю «…»: напоминание (если есть время) + удалить + назад."""
    task_id = task["id"]
    page_suffix = f":{page}" if page is not None else ""
    back_data = f"tasks:page:{page}" if page is not None else f"task:menu_back:{task_id}"

    rows: list[list[InlineKeyboardButton]] = []
    if task.get("time"):
        rows.append([
            InlineKeyboardButton("🔔 Напоминание", callback_data=f"task:remind_menu:{task_id}{page_suffix}"),
            InlineKeyboardButton("🗑 Удалить",     callback_data=f"task:delete:{task_id}{page_suffix}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("🗑 Удалить", callback_data=f"task:delete:{task_id}{page_suffix}"),
        ])
    rows.append([InlineKeyboardButton("◀ Назад", callback_data=back_data)])
    return InlineKeyboardMarkup(rows)


def _remind_picker_keyboard(task: dict, page) -> InlineKeyboardMarkup:
    """Пикер напоминания: 6 пресетов, ❌ только если напоминание уже стоит."""
    task_id = task["id"]
    page_suffix = f":{page}" if page is not None else ""
    back_data = f"task:menu:{task_id}{page_suffix}"

    def btn(label: str, minutes: int) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label, callback_data=f"task:remind:{task_id}:{minutes}{page_suffix}"
        )

    rows = [
        [btn("10 мин", 10), btn("15 мин", 15), btn("30 мин", 30)],
        [btn("1 час",  60), btn("90 мин", 90), btn("2 часа", 120)],
    ]
    last: list[InlineKeyboardButton] = []
    if task.get("notify_before"):
        last.append(InlineKeyboardButton(
            "❌ Отключить", callback_data=f"task:remind:{task_id}:0{page_suffix}"
        ))
    last.append(InlineKeyboardButton("◀ Назад", callback_data=back_data))
    rows.append(last)
    return InlineKeyboardMarkup(rows)


def _format_task_card(task: dict, today: str) -> str:
    """HTML-разметка карточки одной задачи. ➕ — задача добавлена пользователем
    поверх плана (from_schedule=0); ▫️ — пришла из сгенерированного расписания."""
    due = task.get("due_date")
    if due and due < today:
        icon = "⚠️"
    elif due and due == today:
        icon = "📌"
    elif not task.get("from_schedule"):
        icon = "➕"
    else:
        icon = "▫️"

    p = PRIORITY_ICON.get(task.get("priority") or "", "")
    line = f"{p} {icon} <b>{task['title']}</b>" if p else f"{icon} <b>{task['title']}</b>"
    if due:
        when = f"до {due}"
        if task.get("time"):
            when += f" {task['time']}"
        line += f"\n   <i>{when}</i>"
    if task.get("recurring"):
        line += f"  🔄 {task['recurring']}"
    if task.get("description"):
        line += f"\n   {task['description']}"
    return line


def _format_tasks_page(tasks: list[dict], page: int, today: str) -> str:
    """HTML страницы — заголовок и до PAGE_SIZE задач с нумерацией."""
    total_pages = max(1, (len(tasks) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    page_tasks = tasks[start:start + PAGE_SIZE]

    header = f"📋 <b>Активные задачи: {len(tasks)}</b>"
    if total_pages > 1:
        header += f"   <i>стр. {page + 1}/{total_pages}</i>"

    lines = [header, ""]
    for i, t in enumerate(page_tasks, start=1):
        due = t.get("due_date")
        if due and due < today:
            icon = "⚠️"
        elif due and due == today:
            icon = "📌"
        else:
            icon = "▫️"
        p = PRIORITY_ICON.get(t.get("priority") or "", "")
        line = f"<b>{i}.</b> {icon} {p} {t['title']}" if p else f"<b>{i}.</b> {icon} {t['title']}"
        if t.get("time"):
            line += f"  ⏰ {t['time']}"
        if due:
            line += f"  <i>· до {due}</i>"
        if t.get("recurring"):
            line += f"  🔄 {t['recurring']}"
        lines.append(line)
    return "\n".join(lines)


def _tasks_page_keyboard(
    tasks: list[dict], page: int
) -> InlineKeyboardMarkup:
    """Клавиатура страницы: ✅/{i} | …/{i} на задачу + навигация."""
    total_pages = max(1, (len(tasks) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    page_tasks = tasks[start:start + PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    for i, t in enumerate(page_tasks, start=1):
        rows.append([
            InlineKeyboardButton(f"✅ {i}", callback_data=f"task:done:{t['id']}:{page}"),
            InlineKeyboardButton(f"… {i}", callback_data=f"task:menu:{t['id']}:{page}"),
        ])

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"tasks:page:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="tasks:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"tasks:page:{page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton("← Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


async def _send_or_edit_tasks_page(
    db: Database,
    db_user_id: int,
    page: int,
    *,
    target_message=None,
    update=None,
) -> None:
    """
    Универсальная отрисовка страницы задач.
    target_message  — query.message (для edit_text) при ответе на callback
    update          — update (для reply_html) при первом вызове из /tasks
    """
    tasks = await db.get_tasks(db_user_id)
    today = date.today().isoformat()

    if not tasks:
        text = "Активных задач нет! Добавь через /task, /schedule или просто напиши мне 🎉"
        if target_message is not None:
            await target_message.edit_text(text)
        else:
            await update.message.reply_text(text)
        return

    # Если текущая страница вышла за пределы (после удалений) — на последнюю
    total_pages = max(1, (len(tasks) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    text = _format_tasks_page(tasks, page, today)
    keyboard = _tasks_page_keyboard(tasks, page)

    if target_message is not None:
        await target_message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_html(text, reply_markup=keyboard)


async def task_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    user = update.effective_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    args_text = " ".join(ctx.args) if ctx.args else ""
    if not args_text:
        await update.message.reply_text(
            "Как добавить задачу:\n"
            "/task Название задачи                        — без даты\n"
            "/task Название | 2025-12-31                  — с датой\n"
            "/task Название | 2025-12-31 14:00            — с датой и временем\n"
            "/task Зарядка каждый день | daily            — recurring-привычка\n"
            "/task Название | 2025-12-31 | high           — с приоритетом\n"
            "/task Название | | low                       — приоритет без даты"
        )
        return

    # Parse: title [| due_date_or_recurring [| priority]]
    segments = args_text.split("|", 2)
    title = segments[0].strip()
    due_date = None
    time_val = None
    recurring = None
    priority = None

    if len(segments) >= 2:
        extra = segments[1].strip()
        if extra in ("daily", "weekly", "workdays"):
            recurring = extra
        elif extra == "":
            pass  # пустой сегмент — без даты
        else:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?\s*$", extra)
            if m:
                due_date = m.group(1)
                time_val = m.group(2)
            else:
                await update.message.reply_text(
                    f"Не понял дату/повтор: «{extra}»\n"
                    "Форматы: YYYY-MM-DD, YYYY-MM-DD HH:MM, daily, weekly, workdays"
                )
                return

    if len(segments) == 3:
        prio_raw = segments[2].strip().lower()
        if prio_raw in ("high", "medium", "low"):
            priority = prio_raw
        else:
            priority = _PRIORITY_RU.get(prio_raw)
            if priority is None and prio_raw:
                await update.message.reply_text(
                    f"Не понял приоритет: «{segments[2].strip()}»\n"
                    "Допустимые значения: high, medium, low (или высокий, средний, низкий)"
                )
                return

    task_id = await db.add_task(
        db_user["id"],
        title=title,
        due_date=due_date,
        time=time_val,
        recurring=recurring,
        from_schedule=False,
        priority=priority,
    )
    today = date.today().isoformat()
    card = _format_task_card(
        {"id": task_id, "title": title, "due_date": due_date,
         "time": time_val, "recurring": recurring, "from_schedule": 0, "priority": priority},
        today,
    )
    await update.message.reply_html(
        f"✅ Добавлено\n\n{card}",
        reply_markup=_task_keyboard(task_id, has_time=bool(time_val)),
    )


async def tasks_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать активные задачи постранично (по 5 на странице)."""
    db: Database = ctx.bot_data["db"]
    user = update.effective_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)
    await _send_or_edit_tasks_page(db, db_user["id"], page=0, update=update)


async def done_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    ai: AIClient = ctx.bot_data["ai"]
    user = update.effective_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    if not ctx.args:
        await update.message.reply_text("Укажи ID задачи: /done 42")
        return

    try:
        task_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом: /done 42")
        return

    task = await db.complete_task(task_id, db_user["id"])
    if not task:
        await update.message.reply_text(f"Задача #{task_id} не найдена или уже выполнена.")
        return
    await update.message.reply_html(f"✅ Выполнено: <b>{task['title']}</b>")


async def overdue_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    user = update.effective_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    tasks = await db.get_overdue_tasks(db_user["id"])
    if not tasks:
        await update.message.reply_text("Просроченных задач нет! 🎉")
        return

    lines = ["⚠️ <b>Просроченные задачи:</b>\n"]
    for t in tasks:
        lines.append(f"• <code>[{t['id']}]</code> {t['title']} — <i>просрочено {t['due_date']}</i>")
    lines.append("\n<i>Лучше сделать сейчас, чем откладывать дальше 💪</i>")
    await update.message.reply_html("\n".join(lines))


# ── Inline-кнопки на задачах ─────────────────────────────────────────────────

async def handle_task_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик inline-кнопок задач. Понимает три формата callback_data:

      • task:done:<id>            — одиночная карточка после /task
      • task:delete:<id>          — то же
      • task:done:<id>:<page>     — внутри постраничного /tasks
      • task:delete:<id>:<page>   — то же
      • tasks:page:<page>         — перелистывание страницы
      • tasks:noop                — клик по «1/3» (ничего не делать)
    """
    query = update.callback_query
    await query.answer()

    db: Database = ctx.bot_data["db"]
    ai: AIClient = ctx.bot_data["ai"]
    user = query.from_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    parts = query.data.split(":")
    if not parts:
        return

    # ── Перелистывание ─────────────────────────────────────────────────────
    if parts[0] == "tasks":
        if len(parts) >= 2 and parts[1] == "noop":
            return
        if len(parts) >= 3 and parts[1] == "page":
            try:
                page = int(parts[2])
            except ValueError:
                return
            await _send_or_edit_tasks_page(
                db, db_user["id"], page, target_message=query.message
            )
        return

    # ── Действия с задачей ─────────────────────────────────────────────────
    if parts[0] != "task" or len(parts) < 3:
        return
    action = parts[1]
    try:
        task_id = int(parts[2])
    except ValueError:
        return

    # Если в callback есть номер страницы — мы внутри списка, надо перерисовать.
    page: int | None = None
    if len(parts) >= 4:
        try:
            page = int(parts[3])
        except ValueError:
            page = None

    if action == "done":
        task = await db.complete_task(task_id, db_user["id"])
        if not task:
            await query.edit_message_text("Задача не найдена или уже выполнена.")
            return

        # Синхронизация с расписанием — пункты с тем же названием помечаются ✅
        try:
            await db.mark_schedule_items_done_by_title(db_user["id"], task["title"])
        except Exception as e:
            log.warning("Schedule sync after task done failed: %s", e)

        if page is not None:
            await _send_or_edit_tasks_page(
                db, db_user["id"], page, target_message=query.message
            )
            await query.answer(text=f"✅ {task['title']}", show_alert=False)
        else:
            await query.edit_message_text(
                f"✅ <s>{task['title']}</s>",
                parse_mode="HTML",
            )

    elif action == "delete":
        deleted = await db.delete_task(task_id, db_user["id"])
        if not deleted:
            await query.edit_message_text("Задача не найдена.")
            return
        if page is not None:
            await _send_or_edit_tasks_page(
                db, db_user["id"], page, target_message=query.message
            )
            await query.answer(text=f"🗑 {deleted['title']}", show_alert=False)
        else:
            await query.edit_message_text(
                f"🗑 <s>Удалено: {deleted['title']}</s>",
                parse_mode="HTML",
            )

    elif action == "menu":
        task = await db.get_task_by_id(task_id, db_user["id"])
        if not task:
            await query.answer("Задача не найдена.", show_alert=True)
            return
        reminder_info = ""
        if task.get("notify_before"):
            label = _REMIND_LABELS.get(task["notify_before"], f"{task['notify_before']} мин")
            reminder_info = f"\n🔔 Напоминание: за {label}"
        time_info = f"  ⏰ {task['time']}" if task.get("time") else ""
        text = f"<b>{task['title']}</b>{time_info}{reminder_info}\n\nВыбери действие:"
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=_task_submenu_keyboard(task, page),
        )

    elif action == "menu_back":
        task = await db.get_task_by_id(task_id, db_user["id"])
        if not task:
            await query.edit_message_text("Задача не найдена или удалена.")
            return
        today = date.today().isoformat()
        card = _format_task_card(task, today)
        await query.edit_message_text(
            card, parse_mode="HTML",
            reply_markup=_task_keyboard(task_id, has_time=bool(task.get("time"))),
        )

    elif action == "remind_menu":
        task = await db.get_task_by_id(task_id, db_user["id"])
        if not task:
            await query.answer("Задача не найдена.", show_alert=True)
            return
        if not task.get("time"):
            await query.answer(
                "У задачи нет времени — напоминание недоступно.",
                show_alert=True,
            )
            return
        current = ""
        if task.get("notify_before"):
            label = _REMIND_LABELS.get(task["notify_before"], f"{task['notify_before']} мин")
            current = f"\nСейчас: <b>за {label}</b>"
        await query.edit_message_text(
            f"🔔 <b>Напоминание</b>\n<i>{task['title']}</i> ⏰ {task['time']}{current}\n\nЗа сколько напомнить?",
            parse_mode="HTML",
            reply_markup=_remind_picker_keyboard(task, page),
        )

    elif action == "remind":
        # parts: task:remind:<task_id>:<minutes>[:<page>]
        if len(parts) < 4:
            return
        try:
            minutes = int(parts[3])
        except ValueError:
            return
        page = None
        if len(parts) >= 5:
            try:
                page = int(parts[4])
            except ValueError:
                page = None

        notify_before = minutes if minutes > 0 else None
        result = await db.set_task_reminder(task_id, db_user["id"], notify_before)
        if result is None:
            await query.answer(
                "Нельзя: у задачи нет времени или напоминание уходит за полночь.",
                show_alert=True,
            )
            return

        label = _REMIND_LABELS.get(minutes, "отключено" if minutes == 0 else f"{minutes} мин")
        await query.answer(f"🔔 {label} ✓", show_alert=False)
        if page is not None:
            await _send_or_edit_tasks_page(db, db_user["id"], page, target_message=query.message)
        else:
            task_data = await db.get_task_by_id(task_id, db_user["id"])
            if task_data:
                today = date.today().isoformat()
                card = _format_task_card(task_data, today)
                await query.edit_message_text(
                    card, parse_mode="HTML",
                    reply_markup=_task_keyboard(task_id, has_time=True),
                )
            else:
                await query.edit_message_text("✓ Напоминание обновлено.")
