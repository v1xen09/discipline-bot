"""
/settings — пользовательские настройки.

Разделы:
  • Характер бота: soft / neutral / strict / playful
  • Уведомления: утреннее время, вечернее время, авто-напоминания (0–3 слота)
  • Очистка персональной истории

callback_data:
  settings:open                            — главное меню
  settings:persona:<key>                   — установить характер
  settings:notif_open                      — открыть меню уведомлений
  settings:notif_morning:<HH:MM|off>       — установить/выключить утреннее
  settings:notif_evening:<HH:MM|off>       — установить/выключить вечернее
  settings:notif_reminders:<preset>        — выбрать пресет напоминаний
  settings:notif_pick_morning              — показать сетку выбора времени (утро)
  settings:notif_pick_evening              — показать сетку выбора времени (вечер)
  settings:wipe                            — показать подтверждение очистки
  settings:wipe:confirm                    — выполнить очистку
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ai_client import PERSONALITY_LABELS
from database import Database

# Пресеты напоминаний: label → значение notify_reminders (или None = выкл)
_REMINDER_PRESETS = [
    ("Выключено", None),
    ("Раз в день — 12:00", "12:00"),
    ("Два раза — 12:00 и 17:00", "12:00,17:00"),
    ("Три раза — 10:00, 14:00 и 18:00", "10:00,14:00,18:00"),
]

# Сетка выбора часа
_TIME_PRESETS_MORNING = ["06:00", "07:00", "08:00", "09:00", "10:00", "11:00"]
_TIME_PRESETS_EVENING = ["18:00", "19:00", "20:00", "21:00", "22:00", "23:00"]


def _settings_keyboard(current: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    persona_row: list[InlineKeyboardButton] = []
    for key, label in PERSONALITY_LABELS.items():
        marker = "● " if key == current else ""
        persona_row.append(InlineKeyboardButton(
            f"{marker}{label}", callback_data=f"settings:persona:{key}"
        ))
        if len(persona_row) == 2:
            rows.append(persona_row)
            persona_row = []
    if persona_row:
        rows.append(persona_row)

    rows.append([InlineKeyboardButton("🔔 Уведомления", callback_data="settings:notif_open")])
    rows.append([InlineKeyboardButton("🌍 Город (погода)", callback_data="settings:set_city")])
    rows.append([InlineKeyboardButton("🧹 Очистить мою историю", callback_data="settings:wipe")])
    rows.append([InlineKeyboardButton("← Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


async def _render_settings(db: Database, telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    current = await db.get_personality(telegram_id)
    label = PERSONALITY_LABELS.get(current, "🫂 Мягкий")
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Характер бота сейчас: <b>{label}</b>\n\n"
        "Жми на нужный, чтобы переключить — или нажми «Очистить мою историю», "
        "если хочешь начать с чистого листа (твои задачи, расписания, серии и "
        "записи дневника удалятся, но аккаунт останется)."
    )
    return text, _settings_keyboard(current)


def _fix_time(t: str | None) -> str | None:
    """Нормализует старые значения вида '07' → '07:00'."""
    if t and ":" not in t:
        return t.zfill(2) + ":00"
    return t


def _notif_keyboard(settings: dict) -> InlineKeyboardMarkup:
    morning = _fix_time(settings["morning"]) or "выкл"
    evening = _fix_time(settings["evening"]) or "выкл"
    rem = settings["reminders"]
    rem_label = ", ".join(rem.split(",")) if rem else "выкл"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌅 Утро: {morning}  ✏️", callback_data="settings:notif_pick_morning")],
        [InlineKeyboardButton(f"🌙 Вечер: {evening}  ✏️", callback_data="settings:notif_pick_evening")],
        [InlineKeyboardButton(f"🔔 Напоминания: {rem_label}  ✏️", callback_data="settings:notif_reminders_open")],
        [InlineKeyboardButton("← Назад", callback_data="settings:open")],
    ])


def _reminders_keyboard(current: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for label, value in _REMINDER_PRESETS:
        marker = "● " if current == value else ""
        rows.append([InlineKeyboardButton(
            f"{marker}{label}",
            callback_data=f"settings:notif_reminders:{value or 'off'}",
        )])
    rows.append([InlineKeyboardButton("← Назад", callback_data="settings:notif_open")])
    return InlineKeyboardMarkup(rows)


def _time_picker_keyboard(which: str, presets: list[str], current: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in presets:
        marker = "● " if t == current else ""
        row.append(InlineKeyboardButton(f"{marker}{t}", callback_data=f"settings:notif_{which}:{t}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🚫 Выключить", callback_data=f"settings:notif_{which}:off")])
    rows.append([InlineKeyboardButton("← Назад", callback_data="settings:notif_open")])
    return InlineKeyboardMarkup(rows)


async def settings_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    text, keyboard = await _render_settings(db, update.effective_user.id)
    await update.message.reply_html(text, reply_markup=keyboard)


async def handle_settings_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db: Database = ctx.bot_data["db"]
    user = query.from_user
    parts = query.data.split(":")

    if len(parts) < 2 or parts[0] != "settings":
        return

    action = parts[1]

    # Сбрасываем флаги ожидания ввода, если пользователь ушёл из раздела
    if action != "set_city":
        ctx.user_data.pop("awaiting_city", None)
    ctx.user_data.pop("awaiting_note", None)

    if action == "open":
        text, keyboard = await _render_settings(db, user.id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if action == "noop":
        return

    if action == "persona" and len(parts) >= 3:
        key = parts[2]
        if key not in PERSONALITY_LABELS:
            return
        await db.set_personality(user.id, key)
        text, keyboard = await _render_settings(db, user.id)
        label = PERSONALITY_LABELS[key]
        await query.edit_message_text(
            f"Готово, переключился в характер {label}.\n\n" + text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    # ── Уведомления ──────────────────────────────────────────────────────────

    if action == "notif_open":
        settings = await db.get_notification_settings(user.id)
        text = (
            "🔔 <b>Уведомления</b>\n\n"
            "Выбери время утреннего/вечернего сообщения или настрой авто-напоминания.\n"
            "«Выкл» отключает соответствующий тип."
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_notif_keyboard(settings))
        return

    if action == "notif_pick_morning":
        settings = await db.get_notification_settings(user.id)
        kb = _time_picker_keyboard("morning", _TIME_PRESETS_MORNING, current=_fix_time(settings["morning"]))
        await query.edit_message_text(
            "🌅 <b>Утреннее сообщение</b>\n\n"
            "Бот пришлёт сводку задач и мотивацию в выбранное время.",
            parse_mode="HTML", reply_markup=kb,
        )
        return

    if action == "notif_pick_evening":
        settings = await db.get_notification_settings(user.id)
        kb = _time_picker_keyboard("evening", _TIME_PRESETS_EVENING, current=_fix_time(settings["evening"]))
        await query.edit_message_text(
            "🌙 <b>Вечернее сообщение</b>\n\n"
            "Бот подведёт итоги дня в выбранное время.",
            parse_mode="HTML", reply_markup=kb,
        )
        return

    if action == "notif_morning" and len(parts) >= 3:
        # parts[2:] — "07", "00" → собираем обратно в "07:00"
        raw = ":".join(parts[2:])
        new_val = None if raw == "off" else raw
        settings = await db.get_notification_settings(user.id)
        await db.set_notification_settings(user.id, new_val, settings["evening"], settings["reminders"])
        settings["morning"] = new_val
        text = (
            "🔔 <b>Уведомления</b>\n\n"
            f"✅ Утреннее время: <b>{new_val or 'выключено'}</b>"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_notif_keyboard(settings))
        return

    if action == "notif_evening" and len(parts) >= 3:
        raw = ":".join(parts[2:])
        new_val = None if raw == "off" else raw
        settings = await db.get_notification_settings(user.id)
        await db.set_notification_settings(user.id, settings["morning"], new_val, settings["reminders"])
        settings["evening"] = new_val
        text = (
            "🔔 <b>Уведомления</b>\n\n"
            f"✅ Вечернее время: <b>{new_val or 'выключено'}</b>"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_notif_keyboard(settings))
        return

    if action == "notif_reminders_open":
        settings = await db.get_notification_settings(user.id)
        await query.edit_message_text(
            "🔔 <b>Напоминания о задачах</b>\n\n"
            "Бот будет напоминать о невыполненных задачах в выбранное время.\n"
            "Выбери подходящий вариант:",
            parse_mode="HTML",
            reply_markup=_reminders_keyboard(settings["reminders"]),
        )
        return

    if action == "notif_reminders" and len(parts) >= 3:
        raw = ":".join(parts[2:])
        new_val = None if raw == "off" else raw
        settings = await db.get_notification_settings(user.id)
        await db.set_notification_settings(user.id, settings["morning"], settings["evening"], new_val)
        settings["reminders"] = new_val
        await query.edit_message_text(
            "🔔 <b>Напоминания о задачах</b>\n\n"
            f"✅ Сохранено: <b>{new_val or 'выключено'}</b>",
            parse_mode="HTML",
            reply_markup=_reminders_keyboard(new_val),
        )
        return

    # ── Город / погода ───────────────────────────────────────────────────────

    if action == "set_city":
        db2: Database = ctx.bot_data["db"]
        loc = await db2.get_location(user.id)
        current_city = loc.get("city") or "не задан"
        await query.edit_message_text(
            f"🌍 <b>Город для прогноза погоды</b>\n\n"
            f"Сейчас: <b>{current_city}</b>\n\n"
            "Напиши название города (<i>например, Москва</i>) "
            "или отправь геолокацию 📍 прямо в чат.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("← Назад", callback_data="settings:open")]
            ]),
        )
        ctx.user_data["awaiting_city"] = True
        return

    # ── Wipe ─────────────────────────────────────────────────────────────────

    if action == "wipe" and len(parts) == 2:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Да, удалить ВСЁ моё", callback_data="settings:wipe:confirm")],
            [InlineKeyboardButton("← Отмена", callback_data="settings:open")],
        ])
        await query.edit_message_text(
            "Подтверди удаление:\n\n"
            "• задачи (активные и выполненные)\n"
            "• серии и история выполнений\n"
            "• недельные расписания\n"
            "• записи дневника наблюдений\n\n"
            "Аккаунт и выбранный характер бота останутся. Действие необратимо.",
            reply_markup=keyboard,
        )
        return

    if action == "wipe" and len(parts) >= 3 and parts[2] == "confirm":
        counts = await db.clear_user_history(user.id)
        ctx.user_data.pop("chat_history", None)
        ctx.user_data.pop("interaction_count", None)
        ctx.user_data.pop("last_schedule_request", None)

        if not counts:
            await query.edit_message_text("Не нашёл твоих данных — кажется, и удалять нечего.")
            return

        report_lines = ["✅ <b>История очищена.</b>", ""]
        labels = {
            "tasks": "Задач",
            "completions": "Записей о выполнении",
            "streaks": "Серий",
            "schedules": "Расписаний",
            "diary": "Записей дневника",
        }
        for key, label in labels.items():
            n = counts.get(key, 0)
            if n:
                report_lines.append(f"• {label}: {n}")
        if len(report_lines) == 2:
            report_lines.append("• …у тебя и так не было сохранённых данных.")

        await query.edit_message_text("\n".join(report_lines), parse_mode="HTML")
        return
