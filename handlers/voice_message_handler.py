"""
Handles incoming voice messages:
  1. Download OGG from Telegram
  2. Transcribe via Whisper
  3. Use AI to extract intent (add tasks / schedule / done / chat)
  4. Execute intent and reply
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ai_client import AIClient
from database import Database

log = logging.getLogger(__name__)


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    ai: AIClient = ctx.bot_data["ai"]
    vh = ctx.bot_data["voice"]
    user = update.effective_user

    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    processing_msg = await update.message.reply_text("🎤 Слушаю…")

    try:
        # Download voice file
        voice = update.message.voice
        tg_file = await ctx.bot.get_file(voice.file_id)
        file_bytes = await tg_file.download_as_bytearray()

        # Transcribe
        transcript = await vh.transcribe(bytes(file_bytes))
        log.info("Transcribed for %d: %s", user.id, transcript)

        if not transcript:
            await processing_msg.edit_text("Не смог разобрать голосовое. Попробуй ещё раз или напиши текстом.")
            return

        # Show transcript
        await processing_msg.edit_text(f"🗣 <i>«{transcript}»</i>", parse_mode="HTML")

        # AI intent detection
        context = await db.get_user_summary_context(user.id)
        result = ai.process_user_intent(transcript, context)

        intent = result.get("intent", "chat")
        reply_parts = [result.get("reply", "")]

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
                    source="voice",
                    from_schedule=False,
                    priority=task_data.get("priority"),
                )
                ann = title
                if due_date:
                    ann += f" · {due_date}"
                    if time_val:
                        ann += f" {time_val}"
                added.append(f"• {ann}")
            if added:
                reply_parts.append("✅ <b>Добавлены задачи:</b>\n" + "\n".join(added))

        elif intent == "done_tasks":
            from handlers.ai_chat_handler import _apply_by_ids_or_titles
            completed = await _apply_by_ids_or_titles(
                db, db_user["id"],
                ids=result.get("done_task_ids") or [],
                titles=result.get("done_task_titles") or [],
                op=db.complete_task,
            )
            if completed:
                reply_parts.append(
                    "✅ Выполнено:\n" + "\n".join(f"• {c}" for c in completed)
                )

        elif intent == "delete_tasks":
            from handlers.ai_chat_handler import _apply_by_ids_or_titles
            deleted = await _apply_by_ids_or_titles(
                db, db_user["id"],
                ids=result.get("delete_task_ids") or [],
                titles=result.get("delete_task_titles") or [],
                op=db.delete_task,
            )
            if deleted:
                reply_parts.append(
                    "🗑 Удалено:\n" + "\n".join(f"• {c}" for c in deleted)
                )

        elif intent == "schedule" and result.get("schedule_request"):
            from datetime import date, timedelta
            schedule = ai.generate_schedule(transcript, context=context)
            monday = date.today() - timedelta(days=date.today().weekday())
            DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday",
                        "friday", "saturday", "sunday"]
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
            if items:
                deleted, added = await db.replace_week_schedule_tasks(
                    db_user["id"], monday, items
                )
                reply_parts.append(
                    f"📅 План на неделю обновлён · добавлено {added}"
                    + (f", заменено {deleted}" if deleted else "")
                )

        elif intent == "modify_schedule" and result.get("schedule_changes"):
            from handlers.ai_chat_handler import _apply_schedule_changes
            applied, errors = await _apply_schedule_changes(
                db, db_user["id"], result["schedule_changes"]
            )
            if applied:
                reply_parts.append("📅 Изменения:\n" + "\n".join(applied))
            if errors:
                reply_parts.append("⚠️ Не удалось применить:\n" + "\n".join(errors))

        final_reply = "\n\n".join(p for p in reply_parts if p)
        if final_reply:
            await update.message.reply_html(final_reply)

    except Exception as e:
        log.exception("Voice handler error: %s", e)
        await processing_msg.edit_text(
            "Произошла ошибка при обработке голосового сообщения. Попробуй написать текстом."
        )
