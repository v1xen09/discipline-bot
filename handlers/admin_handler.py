"""
/admin — панель диагностики для администратора.

Доступна только пользователю с ADMIN_TELEGRAM_ID из config.
Если ADMIN_TELEGRAM_ID == 0 — команда отключена полностью.

callback_data:
  admin:open     — обновить панель
  admin:weather  — проверить Яндекс.Погода по сохранённым координатам
  admin:morning  — отправить утреннее сообщение прямо сейчас
  admin:evening  — отправить вечернее сообщение прямо сейчас
  admin:reminder — отправить напоминание о задачах прямо сейчас
  admin:diary    — запустить синтез дневника прямо сейчас
  admin:set_city — задать город (awaiting_city = True)
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import Config
from database import Database

log = logging.getLogger(__name__)


def _is_admin(user_id: int, config: Config) -> bool:
    return config.ADMIN_TELEGRAM_ID != 0 and user_id == config.ADMIN_TELEGRAM_ID


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌤 Проверить погоду",         callback_data="admin:weather")],
        [InlineKeyboardButton("🌅 Утреннее сообщение",       callback_data="admin:morning")],
        [InlineKeyboardButton("🌙 Вечернее сообщение",       callback_data="admin:evening")],
        [InlineKeyboardButton("🔔 Напоминание о задачах",    callback_data="admin:reminder")],
        [InlineKeyboardButton("📔 Синтез дневника",          callback_data="admin:diary")],
        [InlineKeyboardButton("📍 Изменить город",           callback_data="admin:set_city")],
        [InlineKeyboardButton("🔄 Обновить статус",          callback_data="admin:open")],
        [InlineKeyboardButton("← Меню",                      callback_data="menu:main")],
    ])


async def _panel_text(ctx: ContextTypes.DEFAULT_TYPE, tg_id: int) -> str:
    db: Database = ctx.bot_data["db"]
    config: Config = ctx.bot_data["config"]
    weather_client = ctx.bot_data.get("weather")

    loc = await db.get_location(tg_id)
    city = loc.get("city") or "не задан"
    has_coords = bool(loc.get("lat") and loc.get("lon"))
    coords_str = f"{loc['lat']:.4f}, {loc['lon']:.4f}" if has_coords else "нет"

    settings = await db.get_notification_settings(tg_id)
    morning = settings["morning"] or "выкл"
    evening = settings["evening"] or "выкл"
    reminders = settings["reminders"] or "выкл"

    weather_ok = weather_client and weather_client.enabled
    weather_icon = "✅" if weather_ok else "❌"
    coords_icon = "✅" if has_coords else "❌"

    return (
        "🛠 <b>Панель администратора</b>\n\n"
        f"{weather_icon} Погода API: {'ключ задан' if weather_ok else 'ключ не задан'}\n"
        f"{coords_icon} Координаты: <code>{coords_str}</code>\n"
        f"📍 Город: <b>{city}</b>\n\n"
        f"🌅 Утро: <b>{morning}</b>\n"
        f"🌙 Вечер: <b>{evening}</b>\n"
        f"🔔 Напоминания: <b>{reminders}</b>\n\n"
        f"🤖 Модель: <code>{config.LMSTUDIO_MODEL}</code>"
    )


async def admin_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = ctx.bot_data["config"]
    if not _is_admin(update.effective_user.id, config):
        return
    text = await _panel_text(ctx, update.effective_user.id)
    await update.message.reply_html(text, reply_markup=_admin_keyboard())


async def handle_admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    config: Config = ctx.bot_data["config"]
    if not _is_admin(query.from_user.id, config):
        await query.answer("Нет доступа", show_alert=True)
        return

    db: Database = ctx.bot_data["db"]
    ai = ctx.bot_data["ai"]
    app = ctx.application
    tg_id = query.from_user.id
    action = query.data.split(":", 1)[1]

    # ── Обновить панель ───────────────────────────────────────────────────────

    if action == "open":
        text = await _panel_text(ctx, tg_id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_admin_keyboard())
        return

    # ── Проверить погоду ──────────────────────────────────────────────────────

    if action == "weather":
        weather_client = ctx.bot_data.get("weather")
        if not weather_client or not weather_client.enabled:
            await query.edit_message_text(
                "❌ <b>YANDEX_WEATHER_KEY не задан</b>\n\nДобавь ключ в .env и перезапусти бота.",
                parse_mode="HTML", reply_markup=_admin_keyboard(),
            )
            return
        loc = await db.get_location(tg_id)
        if not loc.get("lat") or not loc.get("lon"):
            await query.edit_message_text(
                "❌ <b>Координаты не сохранены</b>\n\nСначала задай город через кнопку «📍 Изменить город».",
                parse_mode="HTML", reply_markup=_admin_keyboard(),
            )
            return
        w = await weather_client.get_weather(loc["lat"], loc["lon"])
        if not w:
            await query.edit_message_text(
                "❌ <b>Яндекс.Погода не ответил</b>\n\nПроверь правильность ключа и доступ в интернет.",
                parse_mode="HTML", reply_markup=_admin_keyboard(),
            )
            return
        city = loc.get("city") or f"{loc['lat']:.4f}, {loc['lon']:.4f}"
        text = (
            f"🌤 <b>Погода: {city}</b>\n\n"
            f"Условия: <b>{w['condition']}</b>\n"
            f"Температура: <b>{w['temp']}°C</b> (ощущается {w['feels_like']}°C)\n"
            f"Ветер: {w['wind_speed']} м/с\n\n"
            "✅ API работает корректно"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_admin_keyboard())
        return

    # ── Утреннее сообщение ────────────────────────────────────────────────────

    if action == "morning":
        from scheduler_jobs import _send_morning
        db_user = await db.get_user_by_telegram_id(tg_id)
        personality = await db.get_personality(tg_id)
        await query.edit_message_text("⏳ Отправляю утреннее сообщение…", reply_markup=_admin_keyboard())
        try:
            await _send_morning(app, tg_id, db_user["id"], db, ai, personality)
            status = "✅ Утреннее сообщение отправлено"
        except Exception as e:
            log.exception("Admin: morning send failed")
            status = f"❌ Ошибка: {e}"
        text = f"{status}\n\n" + await _panel_text(ctx, tg_id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_admin_keyboard())
        return

    # ── Вечернее сообщение ────────────────────────────────────────────────────

    if action == "evening":
        from scheduler_jobs import _send_evening
        db_user = await db.get_user_by_telegram_id(tg_id)
        personality = await db.get_personality(tg_id)
        await query.edit_message_text("⏳ Отправляю вечернее сообщение…", reply_markup=_admin_keyboard())
        try:
            await _send_evening(app, tg_id, db_user["id"], db, ai, personality)
            status = "✅ Вечернее сообщение отправлено"
        except Exception as e:
            log.exception("Admin: evening send failed")
            status = f"❌ Ошибка: {e}"
        text = f"{status}\n\n" + await _panel_text(ctx, tg_id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_admin_keyboard())
        return

    # ── Напоминание о задачах ─────────────────────────────────────────────────

    if action == "reminder":
        from scheduler_jobs import _send_reminder
        db_user = await db.get_user_by_telegram_id(tg_id)
        personality = await db.get_personality(tg_id)
        try:
            await _send_reminder(app, tg_id, db_user["id"], db, ai, personality)
            status = "✅ Напоминание отправлено (или пропущено — нет активных задач)"
        except Exception as e:
            log.exception("Admin: reminder send failed")
            status = f"❌ Ошибка: {e}"
        text = f"{status}\n\n" + await _panel_text(ctx, tg_id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_admin_keyboard())
        return

    # ── Синтез дневника ───────────────────────────────────────────────────────

    if action == "diary":
        db_user = await db.get_user_by_telegram_id(tg_id)
        stats = await db.get_completion_stats(db_user["id"], days=1)
        if stats["completions"] == 0:
            text = "ℹ️ Сегодня нет выполненных задач — синтез дневника не нужен\n\n" + await _panel_text(ctx, tg_id)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=_admin_keyboard())
            return
        await query.edit_message_text("⏳ Синтезирую дневник…", reply_markup=_admin_keyboard())
        try:
            context = await db.get_user_summary_context(tg_id)
            events = f"Сегодня пользователь выполнил {stats['completions']} задач."
            entry = ai.synthesize_diary_entry(events, context)
            if entry:
                await db.add_diary_entry(db_user["id"], entry, entry_type="reflection", importance=6)
                status = f"✅ Запись добавлена:\n<i>{entry}</i>"
            else:
                status = "❌ Синтез не удался (модель вернула пустоту)"
        except Exception as e:
            log.exception("Admin: diary synthesis failed")
            status = f"❌ Ошибка: {e}"
        text = f"{status}\n\n" + await _panel_text(ctx, tg_id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_admin_keyboard())
        return

    # ── Задать город ──────────────────────────────────────────────────────────

    if action == "set_city":
        ctx.user_data["awaiting_city"] = True
        loc = await db.get_location(tg_id)
        current = loc.get("city") or "не задан"
        await query.edit_message_text(
            f"📍 <b>Задать город</b>\n\nТекущий: <b>{current}</b>\n\n"
            "Напиши название города или отправь геолокацию 📍:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("← Назад", callback_data="admin:open")]
            ]),
        )
        return
