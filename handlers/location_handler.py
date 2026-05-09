"""Принимает геолокацию Telegram и сохраняет координаты пользователя."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import Database

log = logging.getLogger(__name__)


async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    user = update.effective_user
    loc = update.message.location

    # Сбрасываем флаг ожидания города, если он был (пользователь выбрал геолокацию)
    ctx.user_data.pop("awaiting_city", None)

    await db.set_location(user.id, city=None, lat=loc.latitude, lon=loc.longitude)
    await update.message.reply_text(
        "📍 Геолокация сохранена! Буду брать прогноз погоды по твоим координатам."
    )
