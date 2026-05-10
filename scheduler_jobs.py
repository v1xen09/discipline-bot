"""
Background scheduler jobs using APScheduler.

Jobs:
  • notification_dispatcher — every 1 minute: routes morning/evening/reminder
                              messages per user's personal schedule settings,
                              and fires task-specific reminders (🔔)
  • diary_synthesis         — daily at 23:30: AI memory consolidation
"""

import logging
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

from ai_client import AIClient
from database import Database

log = logging.getLogger(__name__)


def setup_scheduler(app: Application) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    scheduler.add_job(
        _notification_dispatcher,
        IntervalTrigger(minutes=1),
        args=[app],
        id="notification_dispatcher",
        replace_existing=True,
    )
    scheduler.add_job(
        _diary_synthesis_job,
        CronTrigger(hour=23, minute=30),
        args=[app],
        id="diary_synthesis",
        replace_existing=True,
    )

    log.info("Scheduler set up: dispatcher every 1 min, diary synthesis at 23:30")
    return scheduler


# ─── Dispatcher ────────────────────────────────────────────────────────────────

async def _notification_dispatcher(app: Application) -> None:
    """
    Runs every minute. For each user checks whether the current HH:MM matches
    their personal morning / reminder / evening times, then fires the appropriate
    message. Also sends task-specific 🔔 reminders.
    """
    now = datetime.now().strftime("%H:%M")
    today = date.today().isoformat()

    db: Database = app.bot_data["db"]
    ai: AIClient = app.bot_data["ai"]
    user_ids = await db.get_all_user_ids()

    for tg_id in user_ids:
        try:
            db_user = await db.get_user_by_telegram_id(tg_id)
            if not db_user:
                continue
            user_id = db_user["id"]
            settings = await db.get_notification_settings(tg_id)
            personality = await db.get_personality(tg_id)

            if settings["morning"] == now:
                await _send_morning(app, tg_id, user_id, db, ai, personality)

            if settings["reminders"]:
                for slot in settings["reminders"].split(","):
                    if slot.strip() == now:
                        await _send_reminder(app, tg_id, user_id, db, ai, personality)
                        break  # only once per tick even if duplicate slots

            if settings["evening"] == now:
                await _send_evening(app, tg_id, user_id, db, ai, personality)

            tasks_to_remind = await db.get_tasks_to_remind(user_id, now, today)
            if tasks_to_remind:
                lines = "\n".join(
                    f"• {t['title']} в {t['time']}" for t in tasks_to_remind
                )
                await app.bot.send_message(
                    tg_id, f"🔔 <b>Напоминание</b>\n{lines}", parse_mode="HTML"
                )
                await db.mark_tasks_reminded([t["id"] for t in tasks_to_remind])

        except Exception as e:
            log.warning("Dispatcher failed for %d: %s", tg_id, e)


# ─── Message senders ───────────────────────────────────────────────────────────

def _format_weather_block(w: dict, city: str | None) -> str:
    """Форматирует блок погоды с советом по одежде для конца утреннего сообщения."""
    temp = w["temp"]
    feels = w["feels_like"]
    condition = w["condition"]
    wind = w["wind_speed"]

    rain_words = {"дождь", "морось", "ливень", "мокрый снег"}
    snow_words = {"снег", "снегопад", "лёгкий снег"}
    is_rain = any(r in condition for r in rain_words)
    is_snow = any(r in condition for r in snow_words)

    if is_snow or temp < -10:
        advice = "Одевайтесь очень тепло."
    elif is_rain:
        advice = "Возьмите зонт."
    elif temp < 0:
        advice = "На улице мороз — тёплая куртка и шапка."
    elif temp < 10:
        advice = "Оденьтесь тепло."
    elif temp < 18:
        advice = "Лёгкая куртка не помешает."
    elif temp < 26:
        advice = "Комфортно — одевайтесь по погоде."
    else:
        advice = "Жарко — одевайтесь налегке."

    loc_str = f" в {city}" if city else ""
    sign = "+" if temp > 0 else ""
    feels_sign = "+" if feels > 0 else ""
    return (
        f"🌤 <b>Погода{loc_str}:</b> {condition}, {sign}{temp}°C "
        f"(ощущается {feels_sign}{feels}°C), ветер {wind} м/с. {advice}"
    )


async def _send_morning(app, tg_id, user_id, db, ai, personality) -> None:
    context = await db.get_user_summary_context(tg_id)
    upcoming = await db.get_upcoming_tasks(user_id, days=1)
    overdue = await db.get_overdue_tasks(user_id)
    settings = await db.get_notification_settings(tg_id)

    trigger = "overdue" if overdue else "morning"
    text = ai.generate_motivation(context, trigger=trigger, personality=personality)

    # Погода — фиксированный блок в конце, только если пользователь включил
    weather_block = None
    if settings.get("weather"):
        weather_client = app.bot_data.get("weather")
        if weather_client and weather_client.enabled:
            loc = await db.get_location(tg_id)
            if loc.get("lat") and loc.get("lon"):
                w = await weather_client.get_weather(loc["lat"], loc["lon"])
                if w:
                    weather_block = _format_weather_block(w, loc.get("city"))
                    log.info("Morning [%d]: weather appended: %s°C", tg_id, w["temp"])
                else:
                    log.warning("Morning [%d]: weather API returned None", tg_id)
            else:
                log.info("Morning [%d]: weather enabled but no location saved", tg_id)

    if weather_block:
        text += f"\n\n{weather_block}"

    if upcoming:
        task_lines = "\n".join(
            f"  • {t['title']}" + (f" (до {t['due_date']})" if t["due_date"] else "")
            for t in upcoming
        )
        text += f"\n\n📋 <b>Сегодня:</b>\n{task_lines}"

    if overdue:
        shown = overdue[:5]
        rest = len(overdue) - len(shown)
        overdue_lines = "\n".join(
            f"  ⚠️ {t['title']} (просрочено {t['due_date']})" for t in shown
        )
        if rest:
            overdue_lines += f"\n  <i>…и ещё {rest} задач</i>"
        text += f"\n\n<b>Просроченные задачи:</b>\n{overdue_lines}"

    await app.bot.send_message(tg_id, text, parse_mode="HTML")


async def _send_reminder(app, tg_id, user_id, db, ai, personality) -> None:
    overdue = await db.get_overdue_tasks(user_id)
    today_tasks = await db.get_upcoming_tasks(user_id, days=0)

    if not overdue and not today_tasks:
        return

    context = await db.get_user_summary_context(tg_id)
    if overdue:
        lines = "\n".join(
            f"- {t['title']} (просрочено {t['due_date']})" for t in overdue
        )
        context += f"\n\nПросроченные задачи:\n{lines}"
    if today_tasks:
        lines = "\n".join(f"- {t['title']}" for t in today_tasks)
        context += f"\n\nЗадачи на сегодня (ещё не выполнены):\n{lines}"

    trigger = "overdue" if overdue else "reminder"
    text = ai.generate_motivation(context, trigger=trigger, personality=personality)
    await app.bot.send_message(tg_id, text, parse_mode="HTML")


async def _send_evening(app, tg_id, user_id, db, ai, personality) -> None:
    import io
    from datetime import timedelta

    import analytics
    from handlers.analytics_handler import _rate_caption, _rate_trigger

    today = date.today()
    context = await db.get_user_summary_context(tg_id)

    stat = await db.daily_stats(user_id, today)
    comment = ai.generate_motivation(
        context + f"\n\nКоэффициент сегодня: "
                  f"{0 if stat['rate'] is None else int(round(stat['rate'] * 100))}% "
                  f"({stat['completed']} из {stat['planned']}).",
        trigger=_rate_trigger(stat["rate"]),
        personality=personality,
    )

    caption = (
        "🌙 <b>Итог дня</b> · "
        + _rate_caption(stat["rate"], stat["completed"], stat["planned"])
        + (f"\n\n{comment}" if comment else "")
    )

    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    week_stats = await db.daily_stats_range(user_id, monday, sunday)
    week_png = analytics.render_week_chart(week_stats, today)

    await app.bot.send_photo(
        tg_id, photo=io.BytesIO(week_png), caption=caption, parse_mode="HTML"
    )

    is_last_of_month = (today + timedelta(days=1)).day == 1
    if today.weekday() == 6 or is_last_of_month:
        start = today.replace(day=1)
        next_first = (
            date(today.year + 1, 1, 1) if today.month == 12
            else date(today.year, today.month + 1, 1)
        )
        end = next_first - timedelta(days=1)
        month_stats = await db.daily_stats_range(user_id, start, end)
        rated = [s for s in month_stats if s["rate"] is not None]
        avg = sum(s["rate"] for s in rated) / len(rated) if rated else 0
        month_png = analytics.render_month_chart(month_stats, today)
        cap = (
            f"🗓 <b>Месяц</b> · средняя продуктивность <b>{int(round(avg * 100))}%</b>"
            if rated else "🗓 <b>Месяц</b>"
        )
        await app.bot.send_photo(
            tg_id, photo=io.BytesIO(month_png), caption=cap, parse_mode="HTML"
        )


# ─── Diary synthesis ───────────────────────────────────────────────────────────

async def _diary_synthesis_job(app: Application) -> None:
    """AI memory: synthesize today's events into diary entries."""
    db: Database = app.bot_data["db"]
    ai: AIClient = app.bot_data["ai"]
    user_ids = await db.get_all_user_ids()

    for tg_id in user_ids:
        try:
            db_user = await db.get_user_by_telegram_id(tg_id)
            if not db_user:
                continue
            user_id = db_user["id"]
            stats = await db.get_completion_stats(user_id, days=1)
            if stats["completions"] == 0:
                continue

            context = await db.get_user_summary_context(tg_id)
            events = f"Сегодня пользователь выполнил {stats['completions']} задач."
            entry = ai.synthesize_diary_entry(events, context)
            if entry:
                await db.add_diary_entry(user_id, entry, entry_type="reflection", importance=6)
        except Exception as e:
            log.warning("Diary synthesis failed for %d: %s", tg_id, e)
