import io
import logging
from datetime import date, timedelta

from telegram import Update
from telegram.ext import ContextTypes

import analytics
from ai_client import AIClient
from database import Database

log = logging.getLogger(__name__)


def rate_caption(rate: float | None, completed: int, planned: int) -> str:
    if rate is None:
        return "Сегодня задач не было."
    pct = int(round(rate * 100))
    return f"{completed} из {planned} · {pct}%"


def rate_trigger(rate: float | None) -> str:
    """Триггер для generate_motivation, чтобы ИИ подобрал тон под результат."""
    if rate is None:
        return "evening"
    if rate >= 0.8:
        return "rate_high"
    if rate >= 0.5:
        return "rate_mid"
    return "rate_low"


async def today_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    ai: AIClient = ctx.bot_data["ai"]
    user = update.effective_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    today = date.today()
    stat = await db.daily_stats(db_user["id"], today)
    png = analytics.render_today_chart(stat)

    caption = "📊 <b>Сегодня</b> · " + rate_caption(
        stat["rate"], stat["completed"], stat["planned"]
    )

    # Короткий комментарий ИИ под картинкой
    try:
        context = await db.get_user_summary_context(user.id)
        personality = await db.get_personality(user.id)
        comment = ai.generate_motivation(
            context + f"\n\nКоэффициент сегодня: "
                      f"{0 if stat['rate'] is None else int(round(stat['rate'] * 100))}% "
                      f"({stat['completed']} из {stat['planned']}).",
            trigger=rate_trigger(stat["rate"]),
            personality=personality,
        )
        if comment:
            caption += "\n\n" + comment
    except Exception as e:
        log.warning("today commentary failed: %s", e)

    await update.message.reply_photo(
        photo=io.BytesIO(png), caption=caption, parse_mode="HTML",
    )


async def week_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    user = update.effective_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    stats = await db.daily_stats_range(db_user["id"], monday, sunday)

    png = analytics.render_week_chart(stats, today)

    rated = [s for s in stats if s["rate"] is not None]
    if rated:
        avg = sum(s["rate"] for s in rated) / len(rated)
        caption = (
            f"📈 <b>Неделя</b>\n"
            f"Средняя продуктивность: <b>{int(round(avg * 100))}%</b>"
        )
    else:
        caption = "📈 <b>Неделя</b>\nЗа эту неделю задач не было."

    await update.message.reply_photo(
        photo=io.BytesIO(png), caption=caption, parse_mode="HTML",
    )


async def month_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    user = update.effective_user
    db_user = await db.get_or_create_user(user.id, user.username, user.full_name)

    today = date.today()
    start = today.replace(day=1)
    # последний день месяца — переходим на 1-е следующего и отнимаем 1 день
    if today.month == 12:
        next_first = date(today.year + 1, 1, 1)
    else:
        next_first = date(today.year, today.month + 1, 1)
    end = next_first - timedelta(days=1)

    stats = await db.daily_stats_range(db_user["id"], start, end)
    png = analytics.render_month_chart(stats, today)

    rated = [s for s in stats if s["rate"] is not None]
    if rated:
        avg = sum(s["rate"] for s in rated) / len(rated)
        caption = (
            f"🗓 <b>Месяц</b> · "
            f"средняя продуктивность <b>{int(round(avg * 100))}%</b>"
        )
    else:
        caption = "🗓 <b>Месяц</b> · данных пока нет"

    await update.message.reply_photo(
        photo=io.BytesIO(png), caption=caption, parse_mode="HTML",
    )
