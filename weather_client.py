import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_CONDITION_RU: dict[str, str] = {
    "clear": "ясно",
    "partly-cloudy": "малооблачно",
    "cloudy": "облачно",
    "overcast": "пасмурно",
    "drizzle": "морось",
    "light-rain": "лёгкий дождь",
    "rain": "дождь",
    "moderate-rain": "умеренный дождь",
    "heavy-rain": "сильный дождь",
    "continuous-heavy-rain": "проливной дождь",
    "showers": "ливень",
    "wet-snow": "мокрый снег",
    "light-snow": "лёгкий снег",
    "snow": "снег",
    "snow-showers": "снегопад",
    "hail": "град",
    "thunderstorm": "гроза",
    "thunderstorm-with-rain": "гроза с дождём",
    "thunderstorm-with-hail": "гроза с градом",
}

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_WEATHER_URL = "https://api.weather.yandex.ru/v2/forecast"


class WeatherClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def geocode(self, city: str) -> Optional[tuple[float, float]]:
        """Вернуть (lat, lon) по текстовому названию города, или None."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    _GEOCODE_URL,
                    params={"name": city, "count": 1, "language": "ru"},
                )
                resp.raise_for_status()
                results = resp.json().get("results") or []
                if not results:
                    return None
                r = results[0]
                return float(r["latitude"]), float(r["longitude"])
        except Exception as e:
            log.warning("Geocode failed for %r: %s", city, e)
            return None

    async def get_weather(self, lat: float, lon: float) -> Optional[dict]:
        if not self._api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    _WEATHER_URL,
                    params={"lat": lat, "lon": lon, "lang": "ru_RU", "limit": 1, "hours": "false"},
                    headers={"X-Yandex-Weather-Key": self._api_key},
                )
                resp.raise_for_status()
                fact = resp.json().get("fact", {})
                condition_raw = fact.get("condition", "")
                condition = _CONDITION_RU.get(condition_raw, condition_raw)
                return {
                    "temp": fact.get("temp"),
                    "feels_like": fact.get("feels_like"),
                    "condition": condition,
                    "wind_speed": fact.get("wind_speed"),
                }
        except Exception as e:
            log.warning("Yandex Weather request failed (%.4f, %.4f): %s", lat, lon, e)
            return None
