"""
Handles free-form text messages.

Пайплайн:
  1. Прогоняем сообщение через ai.process_user_intent — за один LLM-вызов
     получаем И намерение, И готовый ответ для чата.
  2. Если намерение «действие» (add/done/delete/schedule) — выполняем
     соответствующие операции с БД и поверх них шлём сформированный reply.
  3. Если намерение «chat» — просто шлём reply.

Это означает, что пользователь может управлять задачами обычным текстом:
  «удали отчёт», «отметь что зарядку сделал», «добавь купить хлеб»,
  и не требуется помнить /task /done /delete-команды.
"""

import logging
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ai_client import AIClient
from database import Database

log = logging.getLogger(__name__)


async def handle_text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    ai: AIClient = ctx.bot_data["ai"]
    user = update.effective_user

    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)
    context = await db.get_user_summary_context(user.id)
    personality = await db.get_personality(user.id)

    # Короткая история диалога — нужна, чтобы intent-роутер учёл контекст
    # предыдущих реплик («сделал это» — ссылается на ранее упомянутую задачу).
    history = ctx.user_data.get("chat_history", [])

    text = update.message.text
    result = ai.process_user_intent(
        text, context=context, history=history, personality=personality
    )
    intent = result.get("intent", "chat")
    reply = (result.get("reply") or "").strip()

    extra_lines: list[str] = []

    if intent == "add_tasks" and result.get("tasks"):
        added = []
        for task_data in result["tasks"]:
            title = task_data.get("title", "Задача")
            due_date = task_data.get("due_date")
            time_val = task_data.get("time")
            await db.add_task(
                db_user["id"],
                title=title,
                due_date=due_date,
                time=time_val,
                recurring=task_data.get("recurring"),
                source="text",
                from_schedule=False,
                priority=task_data.get("priority"),
            )
            ann = title
            if due_date:
                ann += f" · {due_date}"
                if time_val:
                    ann += f" {time_val}"
            added.append(ann)
        if added:
            extra_lines.append(
                "✅ Добавлено: " + ", ".join(f"«{t}»" for t in added)
            )

    elif intent == "done_tasks":
        completed = await _apply_by_ids_or_titles(
            db, db_user["id"],
            ids=result.get("done_task_ids") or [],
            titles=result.get("done_task_titles") or [],
            op=db.complete_task,
        )
        if completed:
            # Зеркалим в текущее недельное расписание — пункты с тем же
            # названием получают done=true в /myplan.
            for title in completed:
                try:
                    await db.mark_schedule_items_done_by_title(db_user["id"], title)
                except Exception:
                    pass
            extra_lines.append(
                "✅ Отмечено выполненным: " + ", ".join(f"«{t}»" for t in completed)
            )
        else:
            log.info("done_tasks intent without effect (ids=%s titles=%s)",
                     result.get("done_task_ids"), result.get("done_task_titles"))

    elif intent == "delete_tasks":
        deleted = await _apply_by_ids_or_titles(
            db, db_user["id"],
            ids=result.get("delete_task_ids") or [],
            titles=result.get("delete_task_titles") or [],
            op=db.delete_task,
        )
        if deleted:
            extra_lines.append(
                "🗑 Удалено: " + ", ".join(f"«{t}»" for t in deleted)
            )
        else:
            log.info("delete_tasks intent without effect (ids=%s titles=%s)",
                     result.get("delete_task_ids"), result.get("delete_task_titles"))

    elif intent == "schedule" and result.get("schedule_request"):
        try:
            schedule = ai.generate_schedule(result["schedule_request"], context=context)
            if "raw" in schedule:
                extra_lines.append(
                    "Не получилось разобрать расписание, попробуй уточнить запрос."
                )
            else:
                monday = date.today() - timedelta(days=date.today().weekday())
                # Снимаем плоский список items для replace_week_schedule_tasks
                items: list[dict] = []
                DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday",
                            "friday", "saturday", "sunday"]
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
                    extra_lines.append(
                        "Расписание получилось пустым — уточни запрос."
                    )
                else:
                    deleted, added = await db.replace_week_schedule_tasks(
                        db_user["id"], monday, items
                    )
                    extra_lines.append(
                        f"📅 План на неделю обновлён · добавлено {added}"
                        + (f", заменено старых {deleted}" if deleted else "")
                        + ".\n\nСмотри /myplan."
                    )
                    ctx.user_data["last_schedule_change"] = "regenerate"
        except Exception as e:
            log.warning("Schedule generation from chat failed: %s", e)
            extra_lines.append("Не получилось составить расписание.")

    elif intent == "add_note" and result.get("note_text"):
        note_text = result["note_text"].strip()
        await db.add_note(db_user["id"], note_text, source="ai")
        extra_lines.append(f"📝 Сохранено в заметки: <i>{note_text}</i>")

    elif intent == "delete_note":
        ids = [int(i) for i in (result.get("delete_note_ids") or []) if str(i).isdigit()]
        deleted_count = 0
        for nid in ids:
            if await db.delete_note(nid, db_user["id"]):
                deleted_count += 1
        if deleted_count:
            extra_lines.append(f"🗑 Удалено заметок: {deleted_count}")

    elif intent == "set_priority" and result.get("priority_changes"):
        _PRIO_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}
        updated = []
        for change in result["priority_changes"]:
            try:
                tid = int(change["task_id"])
            except (KeyError, TypeError, ValueError):
                continue
            prio = change.get("priority") or None
            task = await db.set_task_priority(tid, db_user["id"], prio)
            if task:
                icon = _PRIO_ICON.get(prio or "", "–")
                updated.append(f"{icon} {task['title']}")
        if updated:
            extra_lines.append("Приоритет изменён: " + ", ".join(f"«{t}»" for t in updated))

    elif intent == "modify_schedule" and result.get("schedule_changes"):
        applied, errors = await _apply_schedule_changes(
            db, db_user["id"], result["schedule_changes"]
        )
        if applied:
            extra_lines.append("📅 Изменения расписания:\n" + "\n".join(applied))
        if errors:
            extra_lines.append("⚠️ Не удалось применить:\n" + "\n".join(errors))
        if not applied and not errors:
            extra_lines.append("Не нашёл, что именно менять — уточни, пожалуйста.")

    elif intent == "set_reminder" and result.get("reminder_changes"):
        _REMIND_LABELS = {
            10: "10 мин", 15: "15 мин", 30: "30 мин",
            60: "1 час", 90: "90 мин", 120: "2 часа",
        }
        updated = []
        for change in result["reminder_changes"]:
            try:
                tid = int(change["task_id"])
                minutes = int(change.get("minutes") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            task = await db.set_task_reminder(tid, db_user["id"], minutes or None)
            if task is None:
                extra_lines.append(
                    "⚠️ Не смог поставить напоминание — у задачи нет времени. "
                    "Сначала задай время: «встреча с врачом в 15:00»."
                )
            elif task:
                label = _REMIND_LABELS.get(minutes, "отключено" if minutes == 0 else f"{minutes} мин")
                updated.append(f"{'🔔' if minutes else '🔕'} {task['title']}: {label}")
        if updated:
            extra_lines.append("\n".join(updated))

    elif intent == "set_task_time" and result.get("time_changes"):
        updated = []
        for change in result["time_changes"]:
            try:
                tid = int(change["task_id"])
            except (KeyError, TypeError, ValueError):
                continue
            time_val = change.get("time") or None
            task = await db.update_task_time(tid, db_user["id"], time_val)
            if task:
                updated.append(
                    f"⏰ {task['title']}: {'→ ' + time_val if time_val else 'время убрано'}"
                )
        if updated:
            extra_lines.append("\n".join(updated))

    # Собираем итоговый ответ
    final = reply
    if extra_lines:
        final = (final + "\n\n" if final else "") + "\n\n".join(extra_lines)
    if not final.strip():
        final = "Хм, не уловил, что делать. Перефразируй?"

    # Кнопка отката — добавляем под ответом ТОЛЬКО для крупных изменений
    # расписания (regenerate / modify_schedule), которые что-то реально применили.
    reply_markup = None
    if intent in ("schedule", "modify_schedule") and extra_lines:
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("↩ Отменить изменения", callback_data="schedule_undo")
        ]])

    # Историю чата ведём только для intent=chat (для multi-turn диалога).
    # Команды-действия в историю не пишем — иначе модель будет «вспоминать»,
    # что мы уже что-то добавляли, и путаться при повторе.
    if intent == "chat":
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply or final})
        ctx.user_data["chat_history"] = history[-6:]

    await update.message.reply_html(final, reply_markup=reply_markup)

    # Раз в 5 сообщений — синтез наблюдения в дневник
    cnt = ctx.user_data.get("interaction_count", 0) + 1
    ctx.user_data["interaction_count"] = cnt
    if cnt % 5 == 0:
        try:
            entry = ai.synthesize_diary_entry(
                f"Пользователь написал: «{text}» (intent={intent})",
                context,
            )
            if entry:
                await db.add_diary_entry(
                    db_user["id"], entry, entry_type="observation", importance=4
                )
        except Exception as e:
            log.warning("Diary synthesis after chat failed: %s", e)


async def _apply_by_ids_or_titles(
    db: Database,
    user_id: int,
    *,
    ids: list,
    titles: list[str],
    op,
) -> list[str]:
    """
    Применяет op(task_id, user_id) к набору задач.
    Сначала пытается работать по ID (надёжно), если ID нет — по нечёткому
    совпадению в названии (fallback для случая, когда модель забыла ID).
    Возвращает список названий реально затронутых задач.
    """
    affected: list[str] = []
    seen_ids: set[int] = set()

    # 1) По ID — приоритетный путь
    for raw in ids:
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            continue
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        applied = await op(tid, user_id)
        if applied:
            affected.append(applied["title"])

    # 2) По названию — если модель не отдала ID, пробуем матчить
    for title in titles:
        if not title or not title.strip():
            continue
        candidates = await db.find_tasks_by_title(user_id, title, limit=1)
        if not candidates:
            continue
        task = candidates[0]
        if task["id"] in seen_ids:
            continue
        seen_ids.add(task["id"])
        applied = await op(task["id"], user_id)
        if applied:
            affected.append(applied["title"])

    return affected


VALID_DAY_KEYS = {
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
}
DAY_LABELS_RU = {
    "monday": "пн", "tuesday": "вт", "wednesday": "ср", "thursday": "чт",
    "friday": "пт", "saturday": "сб", "sunday": "вс",
}


DAY_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _day_to_iso(day_key: str) -> str:
    """Конкретная дата текущей недели для day_key."""
    monday = date.today() - timedelta(days=date.today().weekday())
    return (monday + timedelta(days=DAY_INDEX[day_key])).isoformat()


async def _apply_schedule_changes(
    db: Database,
    user_id: int,
    changes: list[dict],
) -> tuple[list[str], list[str]]:
    """
    Точечные правки в текущей неделе НА УРОВНЕ ЗАДАЧ:
      • add    — создаёт новую задачу на день (from_schedule=0, отметится «доп»)
      • remove — удаляет задачу по названию (внутри указанного дня)
      • move   — переносит задачу: меняет due_date (и time, если задан)
    """
    applied: list[str] = []
    errors: list[str] = []

    for change in changes:
        op = (change.get("op") or "").lower()

        if op == "add":
            day = (change.get("day") or "").lower()
            if day not in DAY_INDEX:
                errors.append(f"• «{change}» — некорректный день")
                continue
            title = (change.get("task") or "").strip()
            if not title:
                errors.append(f"• Add без названия: {change}")
                continue
            time_val = (change.get("time") or "").strip() or None
            desc = (change.get("description") or "").strip()
            await db.add_task(
                user_id,
                title=title,
                description=desc,
                due_date=_day_to_iso(day),
                time=time_val,
                source="text",
                from_schedule=False,  # «доп. задача» — отметим в /myplan значком
            )
            applied.append(
                f"➕ {DAY_LABELS_RU[day]}"
                + (f" {time_val}" if time_val else "")
                + f" — {title}"
            )

        elif op == "remove":
            day = (change.get("day") or "").lower()
            if day not in DAY_INDEX:
                errors.append(f"• «{change}» — некорректный день")
                continue
            target = (change.get("task") or "").strip()
            if not target:
                errors.append(f"• Remove без названия")
                continue
            day_iso = _day_to_iso(day)
            # Ищем точное совпадение в этом дне
            tasks = await db.find_tasks_by_title(user_id, target, limit=10)
            removed_title = None
            for t in tasks:
                if t.get("due_date") == day_iso:
                    deleted = await db.delete_task(t["id"], user_id)
                    if deleted:
                        removed_title = deleted["title"]
                        break
            if removed_title:
                applied.append(f"➖ {DAY_LABELS_RU[day]} — {removed_title}")
            else:
                errors.append(f"• {DAY_LABELS_RU[day]}: не нашёл «{target}»")

        elif op == "move":
            from_day = (change.get("from_day") or "").lower()
            to_day = (change.get("to_day") or "").lower()
            if from_day not in DAY_INDEX or to_day not in DAY_INDEX:
                errors.append(f"• «{change}» — некорректные дни")
                continue
            target = (change.get("task") or "").strip()
            if not target:
                errors.append(f"• Move без названия")
                continue
            from_iso = _day_to_iso(from_day)
            to_iso = _day_to_iso(to_day)
            new_time = (change.get("new_time") or "").strip() or None

            tasks = await db.find_tasks_by_title(user_id, target, limit=10)
            moved_title = None
            for t in tasks:
                if t.get("due_date") == from_iso:
                    # Обновляем due_date (и time, если задан)
                    import aiosqlite
                    async with aiosqlite.connect(db.path) as conn:
                        if new_time is not None:
                            await conn.execute(
                                "UPDATE tasks SET due_date = ?, time = ? WHERE id = ?",
                                (to_iso, new_time, t["id"]),
                            )
                        else:
                            await conn.execute(
                                "UPDATE tasks SET due_date = ? WHERE id = ?",
                                (to_iso, t["id"]),
                            )
                        await conn.commit()
                    moved_title = t["title"]
                    break
            if moved_title:
                applied.append(
                    f"⇄ {moved_title}: {DAY_LABELS_RU[from_day]} → {DAY_LABELS_RU[to_day]}"
                    + (f" в {new_time}" if new_time else "")
                )
            else:
                errors.append(
                    f"• {DAY_LABELS_RU[from_day]}: не нашёл «{target}» для переноса"
                )

        else:
            errors.append(f"• Неизвестная операция: {op!r}")

    return applied, errors
