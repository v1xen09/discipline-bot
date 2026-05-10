"""
/schedule — interactive weekly schedule creation.
/myplan   — show current week's plan, day by day, with completion checkboxes.

Flow:
  1. /schedule → bot asks what to plan
  2. User replies with free text
  3. AI generates JSON schedule
  4. Bot displays it и сохраняет в БД
  5. По клику пользователь импортирует план в задачи
     (с дедупликацией: повторяющиеся названия → одна recurring-задача)

В /myplan показ постраничный — день недели на сообщение,
у каждого пункта своя кнопка ✅ для отметки. Состояние «выполнено»
хранится прямо в schedule_json (поле "done": true в каждом item).
"""

import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from ai_client import AIClient
from database import Database

log = logging.getLogger(__name__)

AWAITING_SCHEDULE_WEEK = 0
AWAITING_SCHEDULE_REQUEST = 1


def _monday_for_offset(offset: int) -> date:
    today = date.today()
    return today - timedelta(days=today.weekday()) + timedelta(weeks=offset)


def _viewed_monday(ctx) -> date:
    """Возвращает просматриваемую неделю из user_data или текущую."""
    iso = ctx.user_data.get("myplan_week_monday")
    if iso:
        try:
            return date.fromisoformat(iso)
        except ValueError:
            pass
    today = date.today()
    return today - timedelta(days=today.weekday())

DAY_NAMES = {
    "monday": "Понедельник",
    "tuesday": "Вторник",
    "wednesday": "Среда",
    "thursday": "Четверг",
    "friday": "Пятница",
    "saturday": "Суббота",
    "sunday": "Воскресенье",
}
DAY_KEYS = list(DAY_NAMES.keys())
DAY_SHORT = {
    "monday": "Пн", "tuesday": "Вт", "wednesday": "Ср", "thursday": "Чт",
    "friday": "Пт", "saturday": "Сб", "sunday": "Вс",
}


# ── /schedule — генерация плана ──────────────────────────────────────────────

async def schedule_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    m0 = _monday_for_offset(0)
    m1 = _monday_for_offset(1)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📅 Эта неделя ({m0.strftime('%d.%m')}–{(m0 + timedelta(days=6)).strftime('%d.%m')})",
            callback_data="schedule_week_select:0",
        )],
        [InlineKeyboardButton(
            f"📅 Следующая неделя ({m1.strftime('%d.%m')}–{(m1 + timedelta(days=6)).strftime('%d.%m')})",
            callback_data="schedule_week_select:1",
        )],
    ])
    await update.message.reply_html(
        "📅 На какую неделю составить расписание?",
        reply_markup=keyboard,
    )
    return AWAITING_SCHEDULE_WEEK


async def schedule_week_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    offset = int(query.data.split(":")[1])
    ctx.user_data["schedule_week_offset"] = offset
    monday = _monday_for_offset(offset)
    label = "эту" if offset == 0 else "следующую"
    await query.edit_message_text(
        f"📅 Расскажи, что нужно запланировать на {label} неделю "
        f"({monday.strftime('%d.%m')}–{(monday + timedelta(days=6)).strftime('%d.%m')}).\n\n"
        "Например: «Нужно учиться по 2 часа в день, ходить в зал вт/чт/сб»"
    )
    return AWAITING_SCHEDULE_REQUEST


def _resolve_target_monday(schedule: dict, fallback: date) -> date:
    """Определяет итоговый Monday из ответа AI или fallback."""
    raw_date = schedule.get("target_week_start")
    if raw_date:
        try:
            d = date.fromisoformat(raw_date)
            return d - timedelta(days=d.weekday())  # нормализуем к понедельнику
        except ValueError:
            pass
    return fallback


async def receive_schedule_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = ctx.bot_data["db"]
    ai: AIClient = ctx.bot_data["ai"]
    user = update.effective_user

    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)
    context = await db.get_user_summary_context(user.id)

    offset = ctx.user_data.pop("schedule_week_offset", 0)
    target_monday = _monday_for_offset(offset)

    await update.message.reply_text("⏳ Составляю расписание…")
    schedule = ai.generate_schedule(update.message.text, context=context, target_monday=target_monday)

    if "raw" in schedule:
        await update.message.reply_text(
            f"Не смог разобрать JSON, вот сырой ответ:\n\n{schedule['raw']}"
        )
        return ConversationHandler.END

    # AI мог вернуть конкретную дату (произвольная неделя)
    target_monday = _resolve_target_monday(schedule, target_monday)

    items: list[dict] = []
    for day_key in DAY_KEYS:
        for it in schedule.get(day_key, []) or []:
            if not (it.get("task") or "").strip():
                continue
            items.append({
                "day": day_key,
                "time": it.get("time"),
                "task": it["task"],
                "description": it.get("description", ""),
            })
    if not items:
        await update.message.reply_text(
            "Расписание получилось пустым. Попробуй уточнить запрос."
        )
        return ConversationHandler.END

    # Проверяем, есть ли уже задачи на затрагиваемые дни
    affected_days = {it["day"] for it in items}
    existing = await db.get_week_tasks_grouped(db_user["id"], target_monday)
    has_existing = any(existing.get(d) for d in affected_days)

    ctx.user_data["last_schedule_request"] = update.message.text
    ctx.user_data["last_schedule_monday"] = target_monday.isoformat()

    if has_existing:
        ctx.user_data["pending_schedule"] = {
            "items": items,
            "monday": target_monday.isoformat(),
            "request": update.message.text,
        }
        week_label = f"{target_monday.strftime('%d.%m')}–{(target_monday + timedelta(days=6)).strftime('%d.%m')}"
        await update.message.reply_html(
            f"📅 Расписание на <b>{week_label}</b> готово.\n\n"
            "⚠️ У тебя уже есть задачи на эти дни. "
            "Что сделать с существующими задачами?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Заменить", callback_data="schedule_confirm:replace"),
                    InlineKeyboardButton("➕ Добавить", callback_data="schedule_confirm:merge"),
                ],
                [InlineKeyboardButton("❌ Отмена", callback_data="schedule_confirm:no")],
            ]),
        )
        return ConversationHandler.END

    deleted, added = await db.replace_week_schedule_tasks(db_user["id"], target_monday, items)

    week_label = f"{target_monday.strftime('%d.%m')}–{(target_monday + timedelta(days=6)).strftime('%d.%m')}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Открыть по дням", callback_data=f"schedule_day:{_today_key()}")],
        [InlineKeyboardButton("🔁 Перегенерировать", callback_data="schedule_regenerate")],
    ])
    await update.message.reply_html(
        f"📅 <b>План на неделю обновлён</b> ({week_label}).\n"
        f"Добавлено задач: <b>{added}</b>"
        + (f"\nЗаменено старых плановых: <b>{deleted}</b>" if deleted else "")
        + "\n\nОткрой /myplan, чтобы увидеть их по дням.",
        reply_markup=keyboard,
    )
    return ConversationHandler.END


# ── /myplan — постраничный просмотр с чекбоксами ─────────────────────────────

async def myplan_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает план на неделю по дням, плюс блок «Без даты» снизу.
    Источник данных — таблица tasks (а не schedule_json), так что задачи,
    добавленные через /task, текстом или голосом, тоже видны здесь.
    """
    db: Database = ctx.bot_data["db"]
    db_user = await db.get_or_create_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.full_name,
    )
    monday = date.today() - timedelta(days=date.today().weekday())
    ctx.user_data["myplan_week_monday"] = monday.isoformat()
    grouped = await db.get_week_tasks_grouped(db_user["id"], monday)
    await _render_week_plan(grouped, monday, update=update, day_key=_today_key())


# ── Callback handler ─────────────────────────────────────────────────────────

async def handle_schedule_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db: Database = ctx.bot_data["db"]
    ai: AIClient = ctx.bot_data["ai"]
    user = query.from_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    data = query.data

    # ── Подтверждение замены расписания (из чат-интента) ─────────────────
    if data.startswith("schedule_confirm:"):
        action = data.split(":", 1)[1]
        pending = ctx.user_data.pop("pending_schedule", None)

        if action == "no" or not pending:
            await query.edit_message_text("❌ Расписание не изменено.")
            return

        items = pending["items"]
        monday_date = date.fromisoformat(pending["monday"])

        if action == "merge":
            # Добавляем к существующим задачам, ничего не удаляем
            added = 0
            for it in items:
                day_key = (it.get("day") or "").lower()
                if day_key not in DAY_NAMES:
                    continue
                title = (it.get("task") or "").strip()
                if not title:
                    continue
                due = (monday_date + timedelta(days=DAY_KEYS.index(day_key))).isoformat()
                await db.add_task(
                    db_user["id"],
                    title=title,
                    description=it.get("description") or "",
                    due_date=due,
                    time=(it.get("time") or "").strip() or None,
                    source="schedule",
                    from_schedule=True,
                )
                added += 1
            ctx.user_data["last_schedule_monday"] = monday_date.isoformat()
            await query.edit_message_text(
                f"📅 <b>Добавлено к существующему расписанию.</b>\n"
                f"Добавлено задач: <b>{added}</b>\n\nОткрой /myplan, чтобы увидеть их по дням.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📅 Открыть по дням", callback_data=f"schedule_day:{_today_key()}")],
                ]),
            )
            return

        # action == "replace" (или устаревший "yes")
        deleted, added = await db.replace_week_schedule_tasks(db_user["id"], monday_date, items)
        ctx.user_data["last_schedule_monday"] = monday_date.isoformat()
        if pending.get("request"):
            ctx.user_data["last_schedule_request"] = pending["request"]

        await query.edit_message_text(
            f"📅 <b>План на неделю обновлён.</b>\nДобавлено задач: <b>{added}</b>"
            + (f"\nЗаменено старых: <b>{deleted}</b>" if deleted else "")
            + "\n\nОткрой /myplan, чтобы увидеть их по дням.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Открыть по дням", callback_data=f"schedule_day:{_today_key()}")],
                [InlineKeyboardButton("↩ Отменить изменения", callback_data="schedule_undo")],
            ]),
        )
        return

    # ── Откат последнего «большого» изменения расписания ─────────────────
    if data == "schedule_undo":
        monday_iso = ctx.user_data.get("last_schedule_monday") or (date.today() - timedelta(days=date.today().weekday())).isoformat()
        monday_date = date.fromisoformat(monday_iso)
        restored = await db.restore_schedule_previous(db_user["id"], monday_iso)
        if restored is None:
            await query.answer("Откатывать нечего.", show_alert=True)
            return
        # Восстанавливаем реальные задачи в tasks (undo меняет только schedules.json)
        undo_items: list[dict] = []
        for day_key, day_tasks in restored.items():
            for it in (day_tasks or []):
                if (it.get("task") or "").strip():
                    undo_items.append({"day": day_key, **it})
        await db.replace_week_schedule_tasks(
            db_user["id"], monday_date, undo_items, save_snapshot=False
        )
        await query.edit_message_text(
            "↩ <b>Откатил последнее изменение расписания.</b>\n\n"
            + _format_full_schedule(restored),
            parse_mode="HTML",
        )
        return

    # ── Перегенерация ────────────────────────────────────────────────────
    if data == "schedule_regenerate":
        request = ctx.user_data.get("last_schedule_request", "")
        if not request:
            await query.edit_message_text(
                "Не помню оригинальный запрос. Используй /schedule заново."
            )
            return
        await query.edit_message_text("⏳ Перегенерирую…")

        context = await db.get_user_summary_context(user.id)
        monday_iso = ctx.user_data.get("last_schedule_monday")
        monday = date.fromisoformat(monday_iso) if monday_iso else _monday_for_offset(0)
        schedule = ai.generate_schedule(request, context=context, target_monday=monday)
        monday = _resolve_target_monday(schedule, monday)

        items: list[dict] = []
        for day_key in DAY_KEYS:
            for it in schedule.get(day_key, []) or []:
                if not (it.get("task") or "").strip():
                    continue
                items.append({
                    "day": day_key,
                    "time": it.get("time"),
                    "task": it["task"],
                    "description": it.get("description", ""),
                })

        deleted, added = await db.replace_week_schedule_tasks(db_user["id"], monday, items)
        ctx.user_data["last_schedule_monday"] = monday.isoformat()
        week_label = f"{monday.strftime('%d.%m')}–{(monday + timedelta(days=6)).strftime('%d.%m')}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Открыть по дням", callback_data=f"schedule_day:{_today_key()}")],
            [InlineKeyboardButton("🔁 Перегенерировать", callback_data="schedule_regenerate")],
        ])
        await query.message.reply_html(
            f"📅 <b>План на неделю обновлён</b> ({week_label}).\n"
            f"Добавлено задач: <b>{added}</b>"
            + (f"\nЗаменено старых плановых: <b>{deleted}</b>" if deleted else ""),
            reply_markup=keyboard,
        )
        return

    # ── Переключение недели в /myplan ────────────────────────────────────
    if data.startswith("myplan:week:"):
        monday_iso = data[len("myplan:week:"):]
        try:
            monday = date.fromisoformat(monday_iso)
        except ValueError:
            return
        ctx.user_data["myplan_week_monday"] = monday_iso
        grouped = await db.get_week_tasks_grouped(db_user["id"], monday)
        current_monday = date.today() - timedelta(days=date.today().weekday())
        day_key = _today_key() if monday == current_monday else "monday"
        await _render_week_plan(grouped, monday, target_message=query.message, day_key=day_key)
        return

    # ── Переключение дня в /myplan ───────────────────────────────────────
    if data.startswith("schedule_day:"):
        day_key = data.split(":", 1)[1]
        if day_key not in DAY_NAMES:
            return
        monday = _viewed_monday(ctx)
        grouped = await db.get_week_tasks_grouped(db_user["id"], monday)
        await _render_week_plan(grouped, monday, target_message=query.message, day_key=day_key)
        return

    # ── Подменю «…» из /myplan ───────────────────────────────────────────
    if data.startswith("myplan:menu:") and not data.startswith("myplan:menu_back:"):
        parts = data.split(":", 3)
        try:
            task_id = int(parts[2])
        except (IndexError, ValueError):
            return
        day_key = parts[3] if len(parts) > 3 else _today_key()
        if day_key not in DAY_NAMES:
            return

        task = await db.get_task_by_id(task_id, db_user["id"])
        if not task:
            await query.answer("Задача не найдена.", show_alert=True)
            return

        reminder_info = ""
        if task.get("notify_before"):
            label = _MYPLAN_REMIND_LABELS.get(task["notify_before"], f"{task['notify_before']} мин")
            reminder_info = f"\n🔔 Напоминание: за {label}"
        time_info = f"  ⏰ {task['time']}" if task.get("time") else ""
        text = f"<b>{task['title']}</b>{time_info}{reminder_info}\n\nВыбери действие:"
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=_myplan_submenu_keyboard(task, day_key),
        )
        return

    if data.startswith("myplan:menu_back:"):
        parts = data.split(":", 3)
        day_key = parts[3] if len(parts) > 3 else _today_key()
        if day_key not in DAY_NAMES:
            return
        monday = _viewed_monday(ctx)
        grouped = await db.get_week_tasks_grouped(db_user["id"], monday)
        await _render_week_plan(grouped, monday, target_message=query.message, day_key=day_key)
        return

    if data.startswith("myplan:remind_menu:"):
        parts = data.split(":", 3)
        try:
            task_id = int(parts[2])
        except (IndexError, ValueError):
            return
        day_key = parts[3] if len(parts) > 3 else _today_key()

        task = await db.get_task_by_id(task_id, db_user["id"])
        if not task:
            await query.answer("Задача не найдена.", show_alert=True)
            return
        if not task.get("time"):
            await query.answer("У задачи нет времени — напоминание недоступно.", show_alert=True)
            return

        current = ""
        if task.get("notify_before"):
            label = _MYPLAN_REMIND_LABELS.get(task["notify_before"], f"{task['notify_before']} мин")
            current = f"\nСейчас: <b>за {label}</b>"
        await query.edit_message_text(
            f"🔔 <b>Напоминание</b>\n<i>{task['title']}</i> ⏰ {task['time']}{current}\n\nЗа сколько напомнить?",
            parse_mode="HTML",
            reply_markup=_myplan_remind_picker_keyboard(task, day_key),
        )
        return

    if data.startswith("myplan:remind:"):
        # myplan:remind:{task_id}:{minutes}:{day_key}
        parts = data.split(":", 4)
        try:
            task_id = int(parts[2])
            minutes = int(parts[3])
        except (IndexError, ValueError):
            return
        day_key = parts[4] if len(parts) > 4 else _today_key()

        notify_before = minutes if minutes > 0 else None
        result = await db.set_task_reminder(task_id, db_user["id"], notify_before)
        if result is None:
            await query.answer(
                "Нельзя: у задачи нет времени или напоминание уходит за полночь.",
                show_alert=True,
            )
            return

        label = _MYPLAN_REMIND_LABELS.get(minutes, "отключено" if minutes == 0 else f"{minutes} мин")
        await query.answer(f"🔔 {label} ✓", show_alert=False)
        monday = _viewed_monday(ctx)
        grouped = await db.get_week_tasks_grouped(db_user["id"], monday)
        await _render_week_plan(grouped, monday, target_message=query.message, day_key=day_key)
        return

    # ── Отметка выполнения из /myplan ────────────────────────────────────
    if data.startswith("myplan:done:"):
        parts = data.split(":", 3)
        try:
            task_id = int(parts[2])
        except (IndexError, ValueError):
            return
        day_key = parts[3] if len(parts) > 3 else _today_key()

        task = await db.complete_task(task_id, db_user["id"])
        if not task:
            await query.answer("Уже выполнено.", show_alert=True)
            return

        monday = _viewed_monday(ctx)
        grouped = await db.get_week_tasks_grouped(db_user["id"], monday)

        # Инжектируем выполненную задачу обратно с флагом _done для отображения
        done_task = dict(task)
        done_task["_done"] = True
        task_due = done_task.get("due_date")
        if task_due:
            try:
                d = date.fromisoformat(task_due)
                offset = (d - monday).days
                if 0 <= offset <= 6:
                    grouped[DAY_KEYS[offset]].append(done_task)
            except Exception:
                pass
        else:
            grouped["undated"].append(done_task)

        await _render_week_plan(grouped, monday, target_message=query.message, day_key=day_key)
        await query.answer(f"✅ {task['title']}")
        return

    # ── Удаление из /myplan ───────────────────────────────────────────────
    if data.startswith("myplan:delete:"):
        parts = data.split(":", 3)
        try:
            task_id = int(parts[2])
        except (IndexError, ValueError):
            return
        day_key = parts[3] if len(parts) > 3 else _today_key()

        deleted = await db.delete_task(task_id, db_user["id"])
        if not deleted:
            await query.answer("Задача не найдена.", show_alert=True)
            return

        monday = _viewed_monday(ctx)
        grouped = await db.get_week_tasks_grouped(db_user["id"], monday)
        await _render_week_plan(grouped, monday, target_message=query.message, day_key=day_key)
        await query.answer(f"🗑 {deleted['title']}")
        return

    # schedule_done: больше не используется (флаги done живут на уровне задач,
    # отметка идёт через task:done callback из task_handler.py).


# ── Внутренняя кухня ─────────────────────────────────────────────────────────

def _today_key() -> str:
    return DAY_KEYS[date.today().weekday()]


def _format_full_schedule(schedule: dict) -> str:
    """Полный обзор недели (для свежесгенерированного плана и /schedule_regenerate)."""
    today = date.today().isoformat()
    monday = date.today() - timedelta(days=date.today().weekday())

    lines = ["📅 <b>Расписание на неделю</b>\n"]
    for i, key in enumerate(DAY_KEYS):
        day_date = (monday + timedelta(days=i)).isoformat()
        marker = " <i>(сегодня)</i>" if day_date == today else ""
        lines.append(f"<b>{DAY_NAMES[key]}{marker}</b>")

        items = schedule.get(key, [])
        if items:
            for item in items:
                check = "✅" if item.get("done") else "▫️"
                time_str = item.get("time", "")
                task = item.get("task", "")
                desc = item.get("description", "")
                line = f"  {check} {time_str} — {task}"
                if desc:
                    line += f"\n     <i>{desc}</i>"
                lines.append(line)
        else:
            lines.append("  • Свободный день 🌿")
        lines.append("")
    return "\n".join(lines)


# Совместимость со старым названием — оно используется в voice_message_handler
def _format_schedule(schedule: dict) -> str:
    return _format_full_schedule(schedule)


def _format_day_from_tasks(grouped: dict, day_key: str, monday: date) -> str:
    """
    Один день из таблицы tasks. Маркеры:
      ✅ — выполнена (но мы фильтруем completed=0, так что обычно не встретится)
      ▫️ — обычная плановая задача
      ➕ — доп. задача (добавлена пользователем поверх плана, from_schedule=0)
      ⏰ — время указано
    """
    # Дата дня для шапки
    day_idx = DAY_KEYS.index(day_key)
    day_date = monday + timedelta(days=day_idx)
    marker_today = " <i>(сегодня)</i>" if day_date == date.today() else ""
    title = (
        f"📅 <b>{DAY_NAMES[day_key]}</b>{marker_today}  "
        f"<i>{day_date.strftime('%d.%m')}</i>"
    )

    items = grouped.get(day_key, []) or []
    lines = [title, ""]
    if not items:
        lines.append("Свободный день 🌿")
        # Покажем undated-блок прямо тут как «снизу»
        undated = grouped.get("undated", []) or []
        if undated:
            lines += ["", "<b>Без даты:</b>"]
            for i, t in enumerate(undated, start=1):
                lines.append(_format_task_line(i, t, idx_offset=len(items)))
        return "\n".join(lines)

    for i, t in enumerate(items, start=1):
        lines.append(_format_task_line(i, t))

    # Блок задач без даты — внизу плана дня
    undated = grouped.get("undated", []) or []
    if undated:
        lines += ["", "<b>Без даты:</b>"]
        for i, t in enumerate(undated, start=len(items) + 1):
            lines.append(_format_task_line(i, t))

    return "\n".join(lines)


_PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}


def _format_task_line(num: int, task: dict, idx_offset: int = 0) -> str:
    """Одна строка задачи в плане."""
    if task.get("_done"):
        time_part = f"{task['time']} " if task.get("time") else ""
        return f"<b>{num}.</b> ✅ <s>{time_part}{task['title']}</s>"
    bullet = "➕" if not task.get("from_schedule") else "▫️"
    p = _PRIORITY_ICON.get(task.get("priority") or "", "")
    time_part = f"{task['time']} " if task.get("time") else ""
    line = f"<b>{num}.</b> {bullet}{p} {time_part}{task['title']}"
    if task.get("recurring"):
        line += f"  🔄 {task['recurring']}"
    if task.get("description"):
        line += f"\n     <i>{task['description']}</i>"
    return line


def _format_day(schedule: dict, day_key: str) -> str:
    """Один день для интерактивного просмотра."""
    today_key = _today_key()
    marker = " <i>(сегодня)</i>" if day_key == today_key else ""

    items = schedule.get(day_key, []) or []
    lines = [f"📅 <b>{DAY_NAMES[day_key]}</b>{marker}", ""]
    if not items:
        lines.append("Свободный день 🌿")
        return "\n".join(lines)

    for i, item in enumerate(items, start=1):
        check = "✅" if item.get("done") else "▫️"
        time_str = item.get("time", "")
        task = item.get("task", "")
        desc = item.get("description", "")
        head = f"<b>{i}.</b> {check} {time_str} — "
        if item.get("done"):
            head += f"<s>{task}</s>"
        else:
            head += task
        lines.append(head)
        if desc:
            lines.append(f"     <i>{desc}</i>")
    return "\n".join(lines)


def _day_keyboard(schedule: dict, day_key: str) -> InlineKeyboardMarkup:
    """Кнопки для дня: чекбоксы + переключение Пн/Вт/… снизу."""
    items = schedule.get(day_key, []) or []
    rows: list[list[InlineKeyboardButton]] = []

    # Чекбоксы — по 3 в ряд
    row: list[InlineKeyboardButton] = []
    for i, item in enumerate(items):
        emoji = "✅" if item.get("done") else "⬜"
        row.append(InlineKeyboardButton(
            f"{emoji} {i + 1}",
            callback_data=f"schedule_done:{day_key}:{i}",
        ))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Навигация по дням — две строки по 4 и 3
    today_key = _today_key()
    nav_row1: list[InlineKeyboardButton] = []
    nav_row2: list[InlineKeyboardButton] = []
    for i, key in enumerate(DAY_KEYS):
        label = DAY_SHORT[key]
        if key == day_key:
            label = f"·{label}·"
        elif key == today_key:
            label = f"•{label}"
        button = InlineKeyboardButton(label, callback_data=f"schedule_day:{key}")
        if i < 4:
            nav_row1.append(button)
        else:
            nav_row2.append(button)
    rows.append(nav_row1)
    rows.append(nav_row2)

    return InlineKeyboardMarkup(rows)


_MYPLAN_REMIND_LABELS = {
    10: "10 мин", 15: "15 мин", 30: "30 мин",
    60: "1 час",  90: "90 мин", 120: "2 часа",
}


def _week_plan_keyboard(grouped: dict, day_key: str, monday: date) -> InlineKeyboardMarkup:
    """Кнопки для дня: ✅/{i} | …/{i} на каждую задачу + переключатель Пн..Вс + навигация недель."""
    items = list(grouped.get(day_key, []) or [])
    undated = list(grouped.get("undated", []) or [])
    all_items = items + undated

    rows: list[list[InlineKeyboardButton]] = []
    for i, t in enumerate(all_items, start=1):
        if t.get("_done"):
            continue
        rows.append([
            InlineKeyboardButton(f"✅ {i}", callback_data=f"myplan:done:{t['id']}:{day_key}"),
            InlineKeyboardButton(f"… {i}", callback_data=f"myplan:menu:{t['id']}:{day_key}"),
        ])

    today = date.today()
    nav1: list[InlineKeyboardButton] = []
    nav2: list[InlineKeyboardButton] = []
    for i, key in enumerate(DAY_KEYS):
        label = DAY_SHORT[key]
        key_date = monday + timedelta(days=i)
        if key == day_key:
            label = f"·{label}·"
        elif key_date == today:
            label = f"•{label}"
        btn = InlineKeyboardButton(label, callback_data=f"schedule_day:{key}")
        (nav1 if i < 4 else nav2).append(btn)
    rows.append(nav1)
    rows.append(nav2)

    # Навигация по неделям: предыдущая / текущий диапазон / следующая
    prev_m = monday - timedelta(weeks=1)
    next_m = monday + timedelta(weeks=1)
    current_monday = date.today() - timedelta(days=date.today().weekday())
    mid_label = monday.strftime('%d.%m') + "–" + (monday + timedelta(days=6)).strftime('%d.%m')
    if monday == current_monday:
        mid_label = f"·{mid_label}·"
    rows.append([
        InlineKeyboardButton(f"◀ {prev_m.strftime('%d.%m')}", callback_data=f"myplan:week:{prev_m.isoformat()}"),
        InlineKeyboardButton(mid_label, callback_data=f"myplan:week:{current_monday.isoformat()}"),
        InlineKeyboardButton(f"{next_m.strftime('%d.%m')} ▶", callback_data=f"myplan:week:{next_m.isoformat()}"),
    ])

    rows.append([InlineKeyboardButton("← Меню", callback_data="menu:main")])

    return InlineKeyboardMarkup(rows)


def _myplan_submenu_keyboard(task: dict, day_key: str) -> InlineKeyboardMarkup:
    """Подменю «…» для задачи из /myplan."""
    task_id = task["id"]
    rows: list[list[InlineKeyboardButton]] = []
    if task.get("time"):
        rows.append([
            InlineKeyboardButton(
                "🔔 Напоминание",
                callback_data=f"myplan:remind_menu:{task_id}:{day_key}",
            ),
            InlineKeyboardButton(
                "🗑 Удалить",
                callback_data=f"myplan:delete:{task_id}:{day_key}",
            ),
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                "🗑 Удалить",
                callback_data=f"myplan:delete:{task_id}:{day_key}",
            ),
        ])
    rows.append([
        InlineKeyboardButton("◀ Назад", callback_data=f"myplan:menu_back:{task_id}:{day_key}")
    ])
    return InlineKeyboardMarkup(rows)


def _myplan_remind_picker_keyboard(task: dict, day_key: str) -> InlineKeyboardMarkup:
    """Пикер напоминания для задачи в /myplan."""
    task_id = task["id"]

    def btn(label: str, minutes: int) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label, callback_data=f"myplan:remind:{task_id}:{minutes}:{day_key}"
        )

    rows = [
        [btn("10 мин", 10), btn("15 мин", 15), btn("30 мин", 30)],
        [btn("1 час",  60), btn("90 мин", 90), btn("2 часа", 120)],
    ]
    last: list[InlineKeyboardButton] = []
    if task.get("notify_before"):
        last.append(InlineKeyboardButton(
            "❌ Отключить",
            callback_data=f"myplan:remind:{task_id}:0:{day_key}",
        ))
    last.append(InlineKeyboardButton(
        "◀ Назад", callback_data=f"myplan:menu:{task_id}:{day_key}"
    ))
    rows.append(last)
    return InlineKeyboardMarkup(rows)


async def _render_week_plan(
    grouped: dict,
    monday: date,
    *,
    target_message=None,
    update=None,
    day_key: Optional[str] = None,
) -> None:
    """Отрисовать один день недельного плана."""
    if day_key is None:
        day_key = _today_key()
    text = _format_day_from_tasks(grouped, day_key, monday)
    keyboard = _week_plan_keyboard(grouped, day_key, monday)
    if target_message is not None:
        await target_message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_html(text, reply_markup=keyboard)


# _import_schedule_smart удалён — импорт больше не отдельный шаг.
# Сгенерированный план сразу пишется в tasks через replace_week_schedule_tasks.
