"""
/start — register user, send welcome
/help  — command reference
"""

from telegram import Update
from telegram.ext import ContextTypes

from database import Database

HELP_TEXT = """
<b>TManager — твой ИИ помощник по продуктивности 📋</b>

<b>Задачи</b>
/task &lt;название&gt; [| дата YYYY-MM-DD] — добавить задачу
/tasks — список активных задач
/done &lt;id&gt; — отметить задачу выполненной
/overdue — просроченные задачи

<b>Расписание</b>
/schedule — составить расписание на неделю (ИИ спросит детали)
/myplan — показать расписание на эту неделю

<b>Аналитика</b>
/today — итог дня: сколько из планируемого выполнено
/week  — диаграмма по дням недели
/month — календарная сетка месяца с цветовыми квадратами

<b>Настройки</b>
/settings — характер бота (мягкий / нейтральный / требовательный / игривый)
            и очистка твоей истории.

<b>Другое</b>
Просто напиши мне — я пойму 💬
Отправь голосовое сообщение 🎤 — я расшифрую и добавлю задачи
ИИ может сам править расписание: «перенеси алгебру с пт на чт», «добавь в субботу зарядку 9:00», «убери воскресенье». Большие изменения можно откатить кнопкой ↩.

<i>TManager помнит твой прогресс, отслеживает серии и мотивирует двигаться вперёд.</i>
"""


async def start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    user = update.effective_user
    await db.get_or_create_user(
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )
    await update.message.reply_html(
        f"Привет, <b>{user.first_name}</b>! 👋\n\n"
        "Я TManager — твой личный помощник по продуктивности.\n"
        "Я составляю расписание, слушаю голосовые, напоминаю о делах и радуюсь твоим сериям 🔥\n\n"
        "Напиши мне что угодно или отправь /help чтобы узнать всё, что я умею."
    )


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(HELP_TEXT)
