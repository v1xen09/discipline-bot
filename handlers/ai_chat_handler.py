import logging
import re
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest as TgBadRequest
from telegram.ext import ContextTypes

from ai_client import AIClient
from database import Database

log = logging.getLogger(__name__)

_MAX_TG = 4096


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def _send_final(processing_msg, text: str, parse_mode=None, reply_markup=None) -> None:
    if len(text) <= _MAX_TG:
        try:
            await processing_msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except TgBadRequest as e:
            if "can't parse entities" in str(e).lower():
                await processing_msg.edit_text(_strip_html(text), reply_markup=reply_markup)
            else:
                raise
        return
    chunks = [text[i:i + _MAX_TG] for i in range(0, len(text), _MAX_TG)]
    bot = processing_msg.get_bot()
    try:
        await processing_msg.edit_text(chunks[0], parse_mode=parse_mode)
    except TgBadRequest as e:
        if "can't parse entities" in str(e).lower():
            await processing_msg.edit_text(_strip_html(chunks[0]))
        else:
            raise
    for chunk in chunks[1:-1]:
        try:
            await bot.send_message(processing_msg.chat_id, chunk, parse_mode=parse_mode)
        except TgBadRequest as e:
            if "can't parse entities" in str(e).lower():
                await bot.send_message(processing_msg.chat_id, _strip_html(chunk))
            else:
                raise
    try:
        await bot.send_message(
            processing_msg.chat_id, chunks[-1], parse_mode=parse_mode, reply_markup=reply_markup
        )
    except TgBadRequest as e:
        if "can't parse entities" in str(e).lower():
            await bot.send_message(
                processing_msg.chat_id, _strip_html(chunks[-1]), reply_markup=reply_markup
            )
        else:
            raise


async def handle_text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    ai: AIClient = ctx.bot_data["ai"]
    user = update.effective_user

    if ctx.user_data.get("awaiting_city"):
        ctx.user_data.pop("awaiting_city")
        from weather_client import WeatherClient
        weather: WeatherClient = ctx.bot_data.get("weather")
        city_name = (update.message.text or "").strip()
        if weather and city_name:
            coords = await weather.geocode(city_name)
            if coords:
                await db.set_location(user.id, city=city_name, lat=coords[0], lon=coords[1])
                await update.message.reply_text(f"✅ Город сохранён: {city_name}")
            else:
                await update.message.reply_text(
                    f"❌ Не нашёл город «{city_name}». Попробуй другое написание."
                )
        else:
            await update.message.reply_text("Укажи название города текстом.")
        return

    if ctx.user_data.get("awaiting_note"):
        ctx.user_data.pop("awaiting_note")
        db_user = await db.get_or_create_user(user.id, user.username, user.full_name)
        text_note = (update.message.text or "").strip()
        if text_note:
            await db.add_note(db_user["id"], text_note, source="user")
            await update.message.reply_html(f"📝 Сохранено\n\n<i>{text_note}</i>")
        else:
            await update.message.reply_text("Текст заметки пустой — ничего не сохранил.")
        return

    if ctx.user_data.get("awaiting_schedule_edit"):
        from datetime import date as _date, timedelta as _timedelta
        week_start_iso = ctx.user_data.pop("awaiting_schedule_edit")
        db_user = await db.get_or_create_user(user.id, user.username, user.full_name)
        context = await db.get_user_summary_context(user.id)
        try:
            monday_date = _date.fromisoformat(week_start_iso)
        except ValueError:
            await update.message.reply_text("Ошибка — потерял дату расписания. Попробуй /schedule.")
            return
        schedule_row = await db.get_schedule_for_week(db_user["id"], week_start_iso)
        if schedule_row:
            from handlers.schedule_handler import _build_schedule_history_context
            context += "\n\n" + _build_schedule_history_context(
                [{"week_start": week_start_iso, "schedule": schedule_row["schedule"]}]
            )
        processing_msg = await update.message.reply_text("⏳ Перерабатываю расписание…")
        schedule = ai.generate_schedule(update.message.text, context=context, target_monday=monday_date)
        if "raw" in schedule:
            await processing_msg.edit_text("Не смог разобрать ответ. Попробуй ещё раз.")
            return
        await db.save_schedule(db_user["id"], week_start_iso, schedule, keep_history=True)
        from handlers.schedule_handler import _build_schedule_preview, DAY_NAMES
        preview = _build_schedule_preview(schedule, monday_date)
        week_label = f"{monday_date.strftime('%d.%m')}–{(monday_date + _timedelta(days=6)).strftime('%d.%m')}"
        from telegram import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        keyboard = IKM([[
            IKB("✅ Принять", callback_data=f"schedule_accept_proposal:{week_start_iso}"),
            IKB("✏️ Изменить", callback_data=f"schedule_edit_proposal:{week_start_iso}"),
        ]])
        await processing_msg.edit_text(
            f"📅 <b>Обновлённое расписание на {week_label}:</b>\n\n{preview}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)
    context = await db.get_user_summary_context(user.id)
    personality = await db.get_personality(user.id)

    history = ctx.user_data.get("chat_history", [])

    text = update.message.text
    processing_msg = await update.message.reply_text("⏳ Обрабатываю…")
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
            due_date = _normalize_due_date(task_data.get("due_date"))
            time_val = task_data.get("time")
            task_id = await db.add_task(
                db_user["id"],
                title=title,
                due_date=due_date,
                time=time_val,
                recurring=task_data.get("recurring"),
                source="text",
                from_schedule=False,
                priority=task_data.get("priority"),
            )
            notify_before = task_data.get("notify_before")
            if notify_before and time_val:
                try:
                    await db.set_task_reminder(task_id, db_user["id"], int(notify_before))
                except Exception:
                    pass
            ann = title
            if due_date:
                ann += f" · {due_date}"
                if time_val:
                    ann += f" {time_val}"
            if notify_before and time_val:
                ann += f" 🔔-{notify_before}м"
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
            reply = ""
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
            searched = result.get("done_task_titles") or []
            if searched:
                reply = f"❌ Не нашёл задачу «{searched[0]}» — уточни название или открой /tasks."
            else:
                reply = reply or "❌ Не нашёл, что отметить — уточни название."

    elif intent == "delete_tasks":
        deleted = await _apply_by_ids_or_titles(
            db, db_user["id"],
            ids=result.get("delete_task_ids") or [],
            titles=result.get("delete_task_titles") or [],
            op=db.delete_task,
        )
        if deleted:
            reply = ""
            extra_lines.append(
                "🗑 Удалено: " + ", ".join(f"«{t}»" for t in deleted)
            )
        else:
            log.info("delete_tasks intent without effect (ids=%s titles=%s)",
                     result.get("delete_task_ids"), result.get("delete_task_titles"))
            searched = result.get("delete_task_titles") or []
            if searched:
                reply = f"❌ Не нашёл задачу «{searched[0]}» — уточни название или открой /tasks."
            else:
                reply = reply or "❌ Не нашёл, что удалять — уточни название."

    elif intent == "schedule" and result.get("schedule_request"):
        reply = ""  # расписание сохраняется в БД, текст LLM не нужен
        try:
            offset = result.get("schedule_week_offset") or 0
            _today = date.today()
            target_monday = _today - timedelta(days=_today.weekday()) + timedelta(weeks=offset)
            schedule = ai.generate_schedule(text, context=context, target_monday=target_monday)
            if "raw" in schedule:
                extra_lines.append(
                    "Не получилось разобрать расписание, попробуй уточнить запрос."
                )
            else:
                if schedule.get("target_week_start"):
                    try:
                        d = date.fromisoformat(schedule["target_week_start"])
                        target_monday = d - timedelta(days=d.weekday())
                    except ValueError:
                        pass

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
                            "task_type": it.get("type", "task"),
                        })
                if not items:
                    extra_lines.append(
                        "Расписание получилось пустым — уточни запрос."
                    )
                else:
                    affected_days = {item["day"] for item in items}
                    existing = await db.get_week_tasks_grouped(db_user["id"], target_monday)
                    has_existing = any(existing.get(d) for d in affected_days)

                    if has_existing:
                        ctx.user_data["pending_schedule"] = {
                            "items": items,
                            "monday": target_monday.isoformat(),
                        }
                        confirm_text = (reply + "\n\n" if reply else "") + (
                            "⚠️ У тебя уже есть задачи на эти дни. "
                            "Что сделать с существующими задачами?"
                        )
                        await processing_msg.edit_text(
                            confirm_text,
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup([
                                [
                                    InlineKeyboardButton("✅ Заменить", callback_data="schedule_confirm:replace"),
                                    InlineKeyboardButton("➕ Добавить", callback_data="schedule_confirm:merge"),
                                ],
                                [InlineKeyboardButton("❌ Отмена", callback_data="schedule_confirm:no")],
                            ]),
                        )
                        return
                    else:
                        await db.replace_week_schedule_tasks(
                            db_user["id"], target_monday, items
                        )
                        ctx.user_data["last_schedule_monday"] = target_monday.isoformat()
                        ctx.user_data["last_schedule_change"] = "regenerate"
                        extra_lines.append("✅ Расписание на неделю сохранено!\nПосмотреть: /myplan")
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

    final = reply
    if extra_lines:
        final = (final + "\n\n" if final else "") + "\n\n".join(extra_lines)
    if not final.strip():
        final = "Хм, не уловил, что делать. Перефразируй?"

    reply_markup = None
    if intent in ("schedule", "modify_schedule") and extra_lines:
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("↩ Отменить изменения", callback_data="schedule_undo")
        ]])

    # Действия (add/done/delete) в историю не пишем — модель путается при повторе.
    if intent == "chat":
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply or final})
        ctx.user_data["chat_history"] = history[-6:]

    await _send_final(processing_msg, final, parse_mode="HTML", reply_markup=reply_markup)

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
    """Titles — основной путь (надёжнее ID от LLM); IDs — запасной, если titles пуст."""
    affected: list[str] = []
    seen_ids: set[int] = set()

    if titles:
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
    else:
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

    return affected


_WEEKDAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

_RU_RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


def _normalize_due_date(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().lower()
    if s in _RU_RELATIVE_DAYS:
        return (date.today() + timedelta(days=_RU_RELATIVE_DAYS[s])).isoformat()
    try:
        date.fromisoformat(s)
        return s
    except ValueError:
        return None


async def _apply_schedule_changes(
    db: Database,
    user_id: int,
    changes: list[dict],
) -> tuple[list[str], list[str]]:
    applied: list[str] = []
    errors: list[str] = []

    for change in changes:
        op = (change.get("op") or "").lower()

        if op == "add":
            date_iso = (change.get("date") or "").strip()
            if not date_iso:
                errors.append(f"• Add без даты: {change}")
                continue
            try:
                d = date.fromisoformat(date_iso)
            except ValueError:
                errors.append(f"• Add: некорректная дата «{date_iso}»")
                continue
            title = (change.get("task") or "").strip()
            if not title:
                errors.append(f"• Add без названия")
                continue
            time_val = (change.get("time") or "").strip() or None
            desc = (change.get("description") or "").strip()
            await db.add_task(
                user_id,
                title=title,
                description=desc,
                due_date=date_iso,
                time=time_val,
                source="text",
                from_schedule=False,
            )
            day_label = f"{_WEEKDAY_SHORT[d.weekday()]} {d.strftime('%d.%m')}"
            applied.append(
                f"➕ {day_label}"
                + (f" {time_val}" if time_val else "")
                + f" — {title}"
            )

        elif op == "remove":
            date_iso = (change.get("date") or "").strip()
            if not date_iso:
                errors.append(f"• Remove без даты: {change}")
                continue
            try:
                d = date.fromisoformat(date_iso)
            except ValueError:
                errors.append(f"• Remove: некорректная дата «{date_iso}»")
                continue
            target = (change.get("task") or "").strip()
            if not target:
                errors.append(f"• Remove без названия")
                continue
            tasks = await db.find_tasks_by_title(user_id, target, limit=10)
            removed_title = None
            for t in tasks:
                if t.get("due_date") == date_iso:
                    deleted = await db.delete_task(t["id"], user_id)
                    if deleted:
                        removed_title = deleted["title"]
                        break
            day_label = f"{_WEEKDAY_SHORT[d.weekday()]} {d.strftime('%d.%m')}"
            if removed_title:
                applied.append(f"➖ {day_label} — {removed_title}")
            else:
                errors.append(f"• {day_label}: не нашёл «{target}»")

        elif op == "move":
            from_iso = (change.get("from_date") or "").strip()
            to_iso = (change.get("to_date") or "").strip()
            if not from_iso or not to_iso:
                errors.append(f"• Move без дат: {change}")
                continue
            try:
                from_d = date.fromisoformat(from_iso)
                to_d = date.fromisoformat(to_iso)
            except ValueError:
                errors.append(f"• Move: некорректные даты")
                continue
            target = (change.get("task") or "").strip()
            if not target:
                errors.append(f"• Move без названия")
                continue
            new_time = (change.get("new_time") or "").strip() or None

            tasks = await db.find_tasks_by_title(user_id, target, limit=10)
            moved_title = None
            for t in tasks:
                if t.get("due_date") == from_iso:
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
            from_label = f"{_WEEKDAY_SHORT[from_d.weekday()]} {from_d.strftime('%d.%m')}"
            to_label = f"{_WEEKDAY_SHORT[to_d.weekday()]} {to_d.strftime('%d.%m')}"
            if moved_title:
                applied.append(
                    f"⇄ {moved_title}: {from_label} → {to_label}"
                    + (f" в {new_time}" if new_time else "")
                )
            else:
                errors.append(f"• {from_label}: не нашёл «{target}» для переноса")

        else:
            errors.append(f"• Неизвестная операция: {op!r}")

    return applied, errors
