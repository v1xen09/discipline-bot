"""/weather — показать текущую погоду по сохранённому городу."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import Database
from scheduler_jobs import _format_weather_block

log = logging.getLogger(__name__)


async def weather_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    weather_client = ctx.bot_data.get("weather")
    user = update.effective_user

    if not weather_client or not weather_client.enabled:
        await update.message.reply_text(
            "❌ Яндекс.Погода не настроена — ключ YANDEX_WEATHER_KEY не задан."
        )
        return

    loc = await db.get_location(user.id)
    if not loc.get("lat") or not loc.get("lon"):
        await update.message.reply_html(
            "📍 <b>Город не задан.</b>\n\n"
            "Зайди в /settings → Уведомления → Погода в утреннем → задай город,\n"
            "или отправь боту геолокацию 📍.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Настройки", callback_data="settings:set_city")]
            ]),
        )
        return

    w = await weather_client.get_weather(loc["lat"], loc["lon"])
    if not w:
        await update.message.reply_text(
            "❌ Не удалось получить погоду — проверь ключ API или подключение к интернету."
        )
        return

    city = loc.get("city")
    text = _format_weather_block(w, city)
    await update.message.reply_html(text)
