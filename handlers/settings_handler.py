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
    ("Выкл", None),
    ("1×12:00", "12:00"),
    ("2×12+17", "12:00,17:00"),
    ("3×10+14+18", "10:00,14:00,18:00"),
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
    rows.append([InlineKeyboardButton("🧹 Очистить мою историю", callback_data="settings:wipe")])
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


def _notif_keyboard(settings: dict) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    morning = settings["morning"] or "выкл"
    rows.append([
        InlineKeyboardButton(f"🌅 Утро: {morning}", callback_data="settings:notif_pick_morning"),
        InlineKeyboardButton("Выкл" if settings["morning"] else "●Выкл", callback_data="settings:notif_morning:off"),
    ])

    evening = settings["evening"] or "выкл"
    rows.append([
        InlineKeyboardButton(f"🌙 Вечер: {evening}", callback_data="settings:notif_pick_evening"),
        InlineKeyboardButton("Выкл" if settings["evening"] else "●Выкл", callback_data="settings:notif_evening:off"),
    ])

    cur_reminders = settings["reminders"]
    rows.append([InlineKeyboardButton("🔔 Авто-напоминания:", callback_data="settings:noop")])
    preset_row: list[InlineKeyboardButton] = []
    for label, value in _REMINDER_PRESETS:
        marker = "●" if cur_reminders == value else ""
        preset_row.append(InlineKeyboardButton(
            f"{marker}{label}" if marker else label,
            callback_data=f"settings:notif_reminders:{value or 'off'}",
        ))
    rows.append(preset_row)

    rows.append([InlineKeyboardButton("← Назад", callback_data="settings:open")])
    return InlineKeyboardMarkup(rows)


def _time_picker_keyboard(which: str, presets: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in presets:
        row.append(InlineKeyboardButton(t, callback_data=f"settings:notif_{which}:{t}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
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
        kb = _time_picker_keyboard("morning", _TIME_PRESETS_MORNING)
        await query.edit_message_text(
            "🌅 <b>Утреннее сообщение</b>\nВыбери время:", parse_mode="HTML", reply_markup=kb
        )
        return

    if action == "notif_pick_evening":
        kb = _time_picker_keyboard("evening", _TIME_PRESETS_EVENING)
        await query.edit_message_text(
            "🌙 <b>Вечернее сообщение</b>\nВыбери время:", parse_mode="HTML", reply_markup=kb
        )
        return

    if action == "notif_morning" and len(parts) >= 3:
        new_val = None if parts[2] == "off" else parts[2]
        settings = await db.get_notification_settings(user.id)
        await db.set_notification_settings(user.id, new_val, settings["evening"], settings["reminders"])
        settings["morning"] = new_val
        label = new_val or "выкл"
        await query.edit_message_text(
            f"🔔 <b>Уведомления</b>\n\n🌅 Утреннее: <b>{label}</b>",
            parse_mode="HTML",
            reply_markup=_notif_keyboard(settings),
        )
        return

    if action == "notif_evening" and len(parts) >= 3:
        new_val = None if parts[2] == "off" else parts[2]
        settings = await db.get_notification_settings(user.id)
        await db.set_notification_settings(user.id, settings["morning"], new_val, settings["reminders"])
        settings["evening"] = new_val
        label = new_val or "выкл"
        await query.edit_message_text(
            f"🔔 <b>Уведомления</b>\n\n🌙 Вечернее: <b>{label}</b>",
            parse_mode="HTML",
            reply_markup=_notif_keyboard(settings),
        )
        return

    if action == "notif_reminders" and len(parts) >= 3:
        new_val = None if parts[2] == "off" else parts[2]
        settings = await db.get_notification_settings(user.id)
        await db.set_notification_settings(user.id, settings["morning"], settings["evening"], new_val)
        settings["reminders"] = new_val
        label = new_val or "выкл"
        await query.edit_message_text(
            f"🔔 <b>Уведомления</b>\n\n🔔 Напоминания: <b>{label}</b>",
            parse_mode="HTML",
            reply_markup=_notif_keyboard(settings),
        )
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
