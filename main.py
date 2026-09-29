import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from groq import Groq


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "openai/gpt-oss-120b",
).strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("outfit-bot")

groq_client = Groq(
    api_key=GROQ_API_KEY
)

dp = Dispatcher(
    storage=MemoryStorage()
)


# =========================================================
# DATA
# =========================================================

@dataclass
class ClothingItem:
    name: str
    color: str = "не указан"
    category: str = "Другое"


@dataclass
class UserProfile:

    city: Optional[str] = None

    region: Optional[str] = None

    country: Optional[str] = None

    latitude: Optional[float] = None

    longitude: Optional[float] = None

    timezone: str = "Europe/Moscow"

    style: str = "Повседневный"

    scenario: str = "Прогулка"

    cold_tolerance: str = (
        "Нормально переношу холод"
    )

    clothing: str = "Универсальная"

    # Что реально есть
    wardrobe: list[ClothingItem] = field(
        default_factory=list
    )

    # Что пользователь явно отметил как отсутствующее
    unavailable: list[str] = field(
        default_factory=list
    )

    # Что обязательно использовать
    required: list[str] = field(
        default_factory=list
    )

    # Любимые цвета
    preferred_colors: list[str] = field(
        default_factory=list
    )

    updated_at: Optional[str] = None


profiles: dict[int, UserProfile] = {}


class Setup(StatesGroup):

    waiting_city = State()

    waiting_item = State()

    waiting_item_color = State()

    waiting_missing_item = State()


# =========================================================
# PROFILE
# =========================================================

def get_profile(
    user_id: int,
) -> UserProfile:

    if user_id not in profiles:

        profiles[user_id] = UserProfile()

    return profiles[user_id]


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():

    kb = InlineKeyboardBuilder()

    kb.button(
        text="👕 Что надеть?",
        callback_data="outfit",
    )

    kb.button(
        text="🌤 Погода сейчас",
        callback_data="weather",
    )

    kb.button(
        text="📅 День целиком",
        callback_data="day",
    )

    kb.button(
        text="⚙️ Профиль",
        callback_data="profile",
    )

    kb.adjust(1)

    return kb.as_markup()


def profile_keyboard():

    kb = InlineKeyboardBuilder()

    kb.button(
        text="📍 Изменить город",
        callback_data="set_city",
    )

    kb.button(
        text="👕 Мой гардероб",
        callback_data="wardrobe",
    )

    kb.button(
        text="⭐ Обязательные вещи",
        callback_data="required",
    )

    kb.button(
        text="🎨 Любимые цвета",
        callback_data="colors",
    )

    kb.button(
        text="🚫 Чего нет",
        callback_data="unavailable",
    )

    kb.button(
        text="🎨 Стиль",
        callback_data="style",
    )

    kb.button(
        text="🎯 Куда иду",
        callback_data="scenario",
    )

    kb.button(
        text="🥶 Чувствительность к холоду",
        callback_data="cold",
    )

    kb.button(
        text="👔 Тип одежды",
        callback_data="clothing",
    )

    kb.button(
        text="⬅️ Назад",
        callback_data="home",
    )

    kb.adjust(1)

    return kb.as_markup()


def choice_keyboard(
    prefix: str,
    choices: list[tuple[str, str]],
    back: str = "profile",
):

    kb = InlineKeyboardBuilder()

    for label, value in choices:

        kb.button(
            text=label,
            callback_data=f"{prefix}:{value}",
        )

    kb.button(
        text="⬅️ Назад",
        callback_data=back,
    )

    kb.adjust(1)

    return kb.as_markup()


# =========================================================
# HTTP
# =========================================================

async def http_json(
    url: str,
    params: Optional[dict] = None,
    timeout: int = 12,
) -> dict:

    timeout_cfg = aiohttp.ClientTimeout(
        total=timeout
    )

    async with aiohttp.ClientSession(
        timeout=timeout_cfg
    ) as session:

        async with session.get(
            url,
            params=params,
            headers={
                "User-Agent": "OutfitAI/2.2"
            },
        ) as response:

            response.raise_for_status()

            return await response.json()


# =========================================================
# CITY
# =========================================================

async def geocode_city(
    city: str,
) -> Optional[dict]:

    data = await http_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        {
            "name": city,
            "count": 10,
            "language": "ru",
            "format": "json",
        },
    )

    results = data.get(
        "results"
    ) or []

    if not results:
        return None

    city_clean = (
        city
        .strip()
        .casefold()
    )

    # Сначала пытаемся найти точное название
    exact = next(
        (
            result
            for result in results
            if str(
                result.get("name", "")
            ).casefold()
            == city_clean
        ),
        None,
    )

    if exact:
        return exact

    return results[0]


def city_full_name(
    result: dict,
) -> str:

    name = result.get(
        "name",
        "Неизвестный город",
    )

    region = (
        result.get("admin1")
        or result.get("admin2")
        or ""
    )

    country = result.get(
        "country",
        "",
    )

    parts = [name]

    if region and region.casefold() != name.casefold():
        parts.append(region)

    if country and len(parts) == 1:
        parts.append(country)

    return ", ".join(parts)


async def load_and_update_city(
    profile: UserProfile,
    city: str,
) -> bool:

    result = await geocode_city(
        city
    )

    if not result:
        return False

    profile.city = result.get(
        "name"
    )

    profile.region = (
        result.get("admin1")
        or result.get("admin2")
    )

    profile.country = result.get(
        "country"
    )

    profile.latitude = float(
        result["latitude"]
    )

    profile.longitude = float(
        result["longitude"]
    )

    profile.timezone = (
        result.get("timezone")
        or "Europe/Moscow"
    )

    profile.updated_at = (
        datetime.utcnow().isoformat()
    )

    return True


def display_city(
    profile: UserProfile,
) -> str:

    if not profile.city:
        return "не установлен"

    parts = [profile.city]

    if profile.region:
        parts.append(
            profile.region
        )

    return ", ".join(parts)


# =========================================================
# WEATHER
# =========================================================

WEATHER_CODES = {

    0: "ясно",

    1: "преимущественно ясно",

    2: "переменная облачность",

    3: "пасмурно",

    45: "туман",

    48: "туман с изморозью",

    51: "слабая морось",

    53: "морось",

    55: "сильная морось",

    56: "слабая ледяная морось",

    57: "сильная ледяная морось",

    61: "слабый дождь",

    63: "дождь",

    65: "сильный дождь",

    66: "слабый ледяной дождь",

    67: "сильный ледяной дождь",

    71: "слабый снег",

    73: "снег",

    75: "сильный снег",

    77: "снежные зерна",

    80: "слабый ливень",

    81: "ливень",

    82: "сильный ливень",

    85: "слабый снегопад",

    86: "сильный снегопад",

    95: "гроза",

    96: "гроза с градом",

    99: "сильная гроза с градом",
}


def weather_text(
    code: int,
) -> str:

    return WEATHER_CODES.get(
        int(code),
        "неизвестные условия",
    )


async def get_open_meteo(
    profile: UserProfile,
) -> dict:

    return await http_json(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": profile.latitude,

            "longitude": profile.longitude,

            "timezone": "auto",

            "forecast_days": 2,

            "current": (
                "temperature_2m,"
                "apparent_temperature,"
                "precipitation,"
                "rain,"
                "showers,"
                "snowfall,"
                "weather_code,"
                "wind_speed_10m,"
                "wind_gusts_10m,"
                "relative_humidity_2m,"
                "is_day"
            ),

            "hourly": (
                "temperature_2m,"
                "apparent_temperature,"
                "precipitation_probability,"
                "precipitation,"
                "rain,"
                "showers,"
                "snowfall,"
                "weather_code,"
                "wind_speed_10m,"
                "wind_gusts_10m,"
                "relative_humidity_2m"
            ),

            "daily": (
                "weather_code,"
                "temperature_2m_max,"
                "temperature_2m_min,"
                "apparent_temperature_max,"
                "apparent_temperature_min,"
                "precipitation_sum,"
                "rain_sum,"
                "showers_sum,"
                "snowfall_sum,"
                "precipitation_probability_max,"
                "wind_speed_10m_max,"
                "wind_gusts_10m_max,"
                "uv_index_max"
            ),
        },
    )


async def get_wttr(
    profile: UserProfile,
) -> Optional[dict]:

    if not profile.city:
        return None

    try:

        city = quote(
            profile.city
        )

        url = (
            f"https://wttr.in/"
            f"{city}"
        )

        return await http_json(
            url,
            {
                "format": "j1",
                "lang": "ru",
            },
            timeout=8,
        )

    except Exception:

        log.exception(
            "Secondary weather source failed"
        )

        return None


def current_hour_index(
    data: dict,
    profile: UserProfile,
) -> int:

    timezone_name = (
        data.get("timezone")
        or profile.timezone
    )

    try:

        now = datetime.now(
            ZoneInfo(timezone_name)
        )

    except Exception:

        now = datetime.now()

    hours = data[
        "hourly"
    ]["time"]

    if not hours:
        return 0

    current = now.replace(
        tzinfo=None
    )

    best = 0

    best_diff = float(
        "inf"
    )

    for i, value in enumerate(
        hours
    ):

        try:

            dt = datetime.fromisoformat(
                value
            )

            diff = abs(
                (
                    dt - current
                ).total_seconds()
            )

            if diff < best_diff:

                best_diff = diff
                best = i

        except ValueError:

            continue

    return best


def build_weather_snapshot(
    data: dict,
    profile: UserProfile,
    secondary: Optional[dict] = None,
) -> dict:

    i = current_hour_index(
        data,
        profile,
    )

    hourly = data["hourly"]

    current = data["current"]

    daily = data["daily"]

    end = min(
        i + 13,
        len(
            hourly["time"]
        ),
    )

    next_hours = []

    for j in range(i, end):

        next_hours.append(
            {
                "time": hourly["time"][j],

                "temp": hourly[
                    "temperature_2m"
                ][j],

                "feels": hourly[
                    "apparent_temperature"
                ][j],

                "rain_probability": hourly[
                    "precipitation_probability"
                ][j],

                "precipitation": hourly[
                    "precipitation"
                ][j],

                "rain": hourly[
                    "rain"
                ][j],

                "snow": hourly[
                    "snowfall"
                ][j],

                "wind": hourly[
                    "wind_speed_10m"
                ][j],

                "gust": hourly[
                    "wind_gusts_10m"
                ][j],

                "code": hourly[
                    "weather_code"
                ][j],
            }
        )

    # -----------------------------------------------------
    # Проверяем дождь сразу несколькими способами
    # -----------------------------------------------------

    rain_codes = {
        51, 53, 55,
        56, 57,
        61, 63, 65,
        66, 67,
        80, 81, 82,
        95, 96, 99,
    }

    current_code = int(
        current.get(
            "weather_code",
            0,
        )
    )

    current_precip = float(
        current.get(
            "precipitation",
            0,
        ) or 0
    )

    current_rain = float(
        current.get(
            "rain",
            0,
        ) or 0
    )

    first_hours = next_hours[:6]

    next_rain_probability = max(
        [
            int(
                x["rain_probability"]
                or 0
            )
            for x in first_hours
        ]
        or [0]
    )

    next_precipitation = max(
        [
            float(
                x["precipitation"]
                or 0
            )
            for x in first_hours
        ]
        or [0]
    )

    rain_now = (
        current_precip > 0
        or current_rain > 0
        or current_code in rain_codes
    )

    rain_soon = (
        next_rain_probability >= 35
        or next_precipitation > 0
        or any(
            int(x["code"]) in rain_codes
            for x in first_hours
        )
    )

    secondary_weather = {}

    # wttr.in — дополнительная проверка
    if secondary:

        try:

            condition = (
                secondary
                .get("current_condition", [])
            )

            if condition:

                c = condition[0]

                secondary_weather = {
                    "temperature": c.get(
                        "temp_C"
                    ),

                    "feels": c.get(
                        "FeelsLikeC"
                    ),

                    "precipitation": c.get(
                        "precipMM"
                    ),

                    "rain_probability": c.get(
                        "chanceofrain"
                    ),

                    "description": (
                        c.get(
                            "lang_ru",
                            [{}],
                        )[0].get(
                            "value"
                        )
                        if c.get("lang_ru")
                        else c.get(
                            "weatherDesc",
                            [{}],
                        )[0].get(
                            "value",
                            "",
                        )
                    ),

                    "wind": c.get(
                        "windspeedKmph"
                    ),

                    "gust": c.get(
                        "WindGustKmph"
                    ),
                }

        except Exception:

            log.exception(
                "Failed to parse secondary weather"
            )

    secondary_rain = False

    if secondary_weather:

        try:

            secondary_rain = (
                float(
                    secondary_weather.get(
                        "precipitation"
                    )
                    or 0
                )
                > 0
                or int(
                    secondary_weather.get(
                        "rain_probability"
                    )
                    or 0
                )
                >= 35
            )

        except Exception:

            secondary_rain = False

    rain_now = (
        rain_now
        or secondary_rain
    )

    if rain_now:

        rain_status = (
            "Сейчас осадки идут "
            "или есть признаки текущего дождя/ливня."
        )

    elif rain_soon:

        rain_status = (
            "В ближайшие часы возможны "
            "осадки — зонт лучше взять."
        )

    else:

        rain_status = (
            "Существенных признаков дождя "
            "сейчас и в ближайшие часы нет."
        )

    return {

        "timezone": data.get(
            "timezone",
            profile.timezone,
        ),

        "current": {

            "temperature": current.get(
                "temperature_2m"
            ),

            "feels": current.get(
                "apparent_temperature"
            ),

            "precipitation": current.get(
                "precipitation"
            ),

            "rain": current.get(
                "rain"
            ),

            "snow": current.get(
                "snowfall"
            ),

            "wind": current.get(
                "wind_speed_10m"
            ),

            "gust": current.get(
                "wind_gusts_10m"
            ),

            "humidity": current.get(
                "relative_humidity_2m"
            ),

            "code": current_code,

            "description": weather_text(
                current_code
            ),
        },

        "today": {

            "min": daily[
                "temperature_2m_min"
            ][0],

            "max": daily[
                "temperature_2m_max"
            ][0],

            "feels_min": daily[
                "apparent_temperature_min"
            ][0],

            "feels_max": daily[
                "apparent_temperature_max"
            ][0],

            "precip_sum": daily[
                "precipitation_sum"
            ][0],

            "rain_sum": daily[
                "rain_sum"
            ][0],

            "snow_sum": daily[
                "snowfall_sum"
            ][0],

            "precip_probability": daily[
                "precipitation_probability_max"
            ][0],

            "wind_max": daily[
                "wind_speed_10m_max"
            ][0],

            "gust_max": daily[
                "wind_gusts_10m_max"
            ][0],

            "uv": daily[
                "uv_index_max"
            ][0],

            "code": daily[
                "weather_code"
            ][0],

            "description": weather_text(
                daily[
                    "weather_code"
                ][0]
            ),
        },

        "next_hours": next_hours,

        "secondary": secondary_weather,

        "rain_now": rain_now,

        "rain_soon": rain_soon,

        "rain_status": rain_status,

        "next_rain_probability": (
            next_rain_probability
        ),
    }


# =========================================================
# WARDROBE
# =========================================================

COLOR_CHOICES = [
    ("⚫ Чёрный", "чёрный"),
    ("⚪ Белый", "белый"),
    ("🔵 Синий", "синий"),
    ("🔷 Голубой", "голубой"),
    ("🩶 Серый", "серый"),
    ("🟢 Зелёный", "зелёный"),
    ("🔴 Красный", "красный"),
    ("🟡 Жёлтый", "жёлтый"),
    ("🟤 Коричневый", "коричневый"),
    ("🟠 Оранжевый", "оранжевый"),
    ("🟣 Фиолетовый", "фиолетовый"),
    ("🩷 Розовый", "розовый"),
    ("🟦 Другой", "другой"),
]


def wardrobe_text(
    profile: UserProfile,
) -> str:

    if not profile.wardrobe:

        items_text = (
            "Пока ничего нет."
        )

    else:

        groups = {}

        for item in profile.wardrobe:

            groups.setdefault(
                item.category,
                [],
            ).append(
                item
            )

        lines = []

        for category, items in groups.items():

            lines.append(
                f"\n{category}:"
            )

            for item in items:

                marker = "⭐" if (
                    item.name
                    in profile.required
                ) else "•"

                lines.append(
                    f"{marker} "
                    f"{item.name} — "
                    f"{item.color}"
                )

        items_text = "\n".join(
            lines
        )

    missing = (
        "\n".join(
            f"• {x}"
            for x in profile.unavailable
        )
        if profile.unavailable
        else "Ничего не указано."
    )

    required = (
        "\n".join(
            f"⭐ {x}"
            for x in profile.required
        )
        if profile.required
        else "Нет обязательных вещей."
    )

    colors = (
        ", ".join(
            profile.preferred_colors
        )
        if profile.preferred_colors
        else "не заданы"
    )

    return (
        "👕 МОЙ ГАРДЕРОБ\n\n"

        f"Есть:\n"
        f"{items_text}\n\n"

        f"⭐ Обязательно:\n"
        f"{required}\n\n"

        f"🚫 Нет:\n"
        f"{missing}\n\n"

        f"🎨 Любимые цвета:\n"
        f"{colors}"
    )


def wardrobe_keyboard(
    profile: UserProfile,
):

    kb = InlineKeyboardBuilder()

    kb.button(
        text="➕ Добавить вещь",
        callback_data="wardrobe_add",
    )

    if profile.wardrobe:

        kb.button(
            text="🗑 Удалить вещь",
            callback_data="wardrobe_delete",
        )

    kb.button(
        text="⬅️ Назад",
        callback_data="profile",
    )

    kb.adjust(1)

    return kb.as_markup()


def category_from_item(
    item_name: str,
) -> str:

    text = item_name.casefold()

    if any(
        x in text
        for x in (
            "футбол",
            "майка",
            "рубаш",
            "лонгслив",
            "поло",
        )
    ):
        return "👕 Верх"

    if any(
        x in text
        for x in (
            "джинс",
            "брюк",
            "шорт",
            "штаны",
            "карго",
        )
    ):
        return "👖 Низ"

    if any(
        x in text
        for x in (
            "худи",
            "свитер",
            "свитшот",
            "кофта",
        )
    ):
        return "🧶 Тёплый слой"

    if any(
        x in text
        for x in (
            "куртк",
            "пальто",
            "ветровк",
            "пуховик",
            "жилет",
        )
    ):
        return "🧥 Верхняя одежда"

    if any(
        x in text
        for x in (
            "кроссов",
            "кед",
            "ботин",
            "ботинок",
            "туфл",
            "сандал",
        )
    ):
        return "👟 Обувь"

    if any(
        x in text
        for x in (
            "шапк",
            "кепк",
            "панам",
            "шарф",
            "перчат",
        )
    ):
        return "🧢 Аксессуары"

    return "👔 Другое"


def wardrobe_delete_keyboard(
    profile: UserProfile,
):

    kb = InlineKeyboardBuilder()

    for i, item in enumerate(
        profile.wardrobe
    ):

        kb.button(
            text=(
                f"🗑 {item.name} "
                f"— {item.color}"
            ),
            callback_data=f"delitem:{i}",
        )

    kb.button(
        text="⬅️ Назад",
        callback_data="wardrobe",
    )

    kb.adjust(1)

    return kb.as_markup()


def required_keyboard(
    profile: UserProfile,
):

    kb = InlineKeyboardBuilder()

    for i, item in enumerate(
        profile.wardrobe
    ):

        marker = (
            "⭐"
            if item.name
            in profile.required
            else "○"
        )

        kb.button(
            text=(
                f"{marker} "
                f"{item.name} — "
                f"{item.color}"
            ),
            callback_data=f"reqitem:{i}",
        )

    kb.button(
        text="⬅️ Назад",
        callback_data="wardrobe",
    )

    kb.adjust(1)

    return kb.as_markup()


def colors_keyboard(
    profile: UserProfile,
):

    kb = InlineKeyboardBuilder()

    for label, color in COLOR_CHOICES:

        marker = (
            "✅"
            if color
            in profile.preferred_colors
            else "○"
        )

        kb.button(
            text=f"{marker} {label}",
            callback_data=f"prefcolor:{color}",
        )

    kb.button(
        text="🧹 Очистить цвета",
        callback_data="clearcolors",
    )

    kb.button(
        text="⬅️ Назад",
        callback_data="profile",
    )

    kb.adjust(2)

    return kb.as_markup()


# =========================================================
# PROFILE TEXT
# =========================================================

def profile_text(
    profile: UserProfile,
) -> str:

    city = display_city(
        profile
    )

    required_count = len(
        profile.required
    )

    wardrobe_count = len(
        profile.wardrobe
    )

    colors = (
        ", ".join(
            profile.preferred_colors
        )
        if profile.preferred_colors
        else "не заданы"
    )

    return (
        "⚙️ ТВОЙ ПРОФИЛЬ\n\n"

        f"📍 Город: {city}\n"

        f"🎨 Стиль: {profile.style}\n"

        f"🎯 Куда: {profile.scenario}\n"

        f"🥶 Холод: "
        f"{profile.cold_tolerance}\n"

        f"👔 Тип одежды: "
        f"{profile.clothing}\n\n"

        f"👕 В гардеробе: "
        f"{wardrobe_count} вещей\n"

        f"⭐ Обязательных: "
        f"{required_count}\n"

        f"🎨 Любимые цвета: "
        f"{colors}"
    )


# =========================================================
# WEATHER MESSAGE
# =========================================================

def format_weather_message(
    profile: UserProfile,
    snapshot: dict,
) -> str:

    current = snapshot[
        "current"
    ]

    today = snapshot[
        "today"
    ]

    rain_status = snapshot[
        "rain_status"
    ]

    return (
        f"🌤 {display_city(profile)}\n\n"

        f"🌡 Сейчас: "
        f"{current['temperature']}°C\n"

        f"🥶 Ощущается: "
        f"{current['feels']}°C\n"

        f"☁️ "
        f"{current['description'].capitalize()}\n"

        f"💨 Ветер: "
        f"{current['wind']} км/ч, "
        f"порывы до "
        f"{current['gust']} км/ч\n"

        f"💧 Влажность: "
        f"{current['humidity']}%\n\n"

        f"🌧 {rain_status}\n"

        f"📊 Вероятность осадков сегодня: "
        f"{today['precip_probability']}%\n"

        f"🌡 День: "
        f"{today['min']}…"
        f"{today['max']}°C\n"

        f"☀️ UV: "
        f"{today['uv']}"
    )


# =========================================================
# WEATHER SNAPSHOT FOR AI
# =========================================================

def compact_weather_text(
    snapshot: dict,
) -> str:

    current = snapshot[
        "current"
    ]

    today = snapshot[
        "today"
    ]

    lines = [

        f"Сейчас: "
        f"{current['temperature']}°C.",

        f"Ощущается: "
        f"{current['feels']}°C.",

        f"Состояние: "
        f"{current['description']}.",

        f"Ветер: "
        f"{current['wind']} км/ч.",

        f"Порывы: "
        f"{current['gust']} км/ч.",

        f"Влажность: "
        f"{current['humidity']}%.",

        f"Осадки сейчас: "
        f"{current['precipitation']} мм.",

        f"Дождь сейчас: "
        f"{current['rain']} мм.",

        f"Снег сейчас: "
        f"{current['snow']} мм.",

        f"Сегодня: "
        f"{today['min']}…"
        f"{today['max']}°C.",

        f"Вероятность осадков сегодня: "
        f"{today['precip_probability']}%.",

        f"Максимальный ветер сегодня: "
        f"{today['wind_max']} км/ч.",

        f"Осадки сейчас/скоро: "
        f"{'ДА' if snapshot['rain_now'] else 'НЕТ'}.",

        f"Осадки в ближайшие часы: "
        f"{'ВОЗМОЖНЫ' if snapshot['rain_soon'] else 'маловероятны'}.",

        f"Ближайшая вероятность осадков: "
        f"{snapshot['next_rain_probability']}%.",

        f"Вывод по дождю: "
        f"{snapshot['rain_status']}",
    ]

    lines.append(
        "Ближайшие часы:"
    )

    for hour in snapshot[
        "next_hours"
    ][:8]:

        lines.append(
            f"{hour['time'][11:16]} — "
            f"{hour['temp']}°C, "
            f"ощущается "
            f"{hour['feels']}°C, "
            f"осадки "
            f"{hour['rain_probability']}%, "
            f"{weather_text(hour['code'])}."
        )

    secondary = snapshot.get(
        "secondary"
    )

    if secondary:

        lines.append(
            "Дополнительный источник "
            "погоды:"
        )

        lines.append(
            f"Температура: "
            f"{secondary.get('temperature')}, "
            f"ощущается "
            f"{secondary.get('feels')}, "
            f"осадки "
            f"{secondary.get('precipitation')}, "
            f"вероятность дождя "
            f"{secondary.get('rain_probability')}%."
        )

    return "\n".join(
        lines
    )


# =========================================================
# GROQ
# =========================================================

def clean_ai_text(
    text: str,
) -> str:

    # Убираем Markdown-жирность
    text = text.replace(
        "**",
        "",
    )

    text = text.replace(
        "__",
        "",
    )

    # Убираем тройные backticks
    text = text.replace(
        "```",
        "",
    )

    # Иногда модель начинает добавлять
    # ненужные заголовки Markdown.
    text = re.sub(
        r"^#+\s*",
        "",
        text,
        flags=re.MULTILINE,
    )

    return text.strip()


def wardrobe_for_ai(
    profile: UserProfile,
) -> str:

    if not profile.wardrobe:

        return (
            "Гардероб не заполнен. "
            "Не придумывай конкретные вещи "
            "как будто они точно есть."
        )

    available = []

    for item in profile.wardrobe:

        required = (
            "ОБЯЗАТЕЛЬНАЯ"
            if item.name
            in profile.required
            else "обычная"
        )

        available.append(
            f"- {item.name}; "
            f"цвет: {item.color}; "
            f"категория: {item.category}; "
            f"статус: {required}"
        )

    unavailable = []

    for item in profile.unavailable:

        unavailable.append(
            f"- {item}"
        )

    text = (
        "ЕСТЬ:\n"
        + "\n".join(available)
    )

    if unavailable:

        text += (
            "\n\nТОЧНО НЕТ:\n"
            + "\n".join(unavailable)
        )

    if profile.required:

        text += (
            "\n\nОБЯЗАТЕЛЬНО "
            "ИСПОЛЬЗОВАТЬ:\n"
            + "\n".join(
                f"- {x}"
                for x in profile.required
            )
        )

    if profile.preferred_colors:

        text += (
            "\n\nПРЕДПОЧТИТЕЛЬНЫЕ ЦВЕТА:\n"
            + ", ".join(
                profile.preferred_colors
            )
        )

    return text


def choose_outfit_style_hint(
    profile: UserProfile,
) -> str:

    hints = {

        "Повседневный": [
            "сделай простой повседневный образ",
            "собери удобный комплект без перегруза",
            "используй базовые вещи, но меняй комбинации",
        ],

        "Спортивный": [
            "используй спортивную эстетику",
            "сделай акцент на удобстве и свободной посадке",
            "собери спортивный комплект",
        ],

        "Минимализм": [
            "используй спокойную цветовую комбинацию",
            "сделай образ чистым и лаконичным",
            "не перегружай образ большим количеством цветов",
        ],

        "Нарядный": [
            "сделай образ более аккуратным и собранным",
            "используй более строгую комбинацию вещей",
            "подбери более опрятный вариант",
        ],

        "Streetwear": [
            "собери современный streetwear-комплект",
            "используй многослойность, если позволяет погода",
            "сделай силуэт более расслабленным",
        ],
    }

    values = hints.get(
        profile.style,
        hints["Повседневный"],
    )

    return random.choice(
        values
    )


async def ask_groq(
    profile: UserProfile,
    snapshot: dict,
    request_type: str = "outfit",
) -> str:

    weather = compact_weather_text(
        snapshot
    )

    wardrobe = wardrobe_for_ai(
        profile
    )

    style_hint = choose_outfit_style_hint(
        profile
    )

    if request_type == "day":

        task = (
            "Составь образ на весь день. "
            "Учитывай изменение температуры "
            "и предложи слой, который можно "
            "снять или добавить."
        )

    else:

        task = (
            "Подбери один основной образ "
            "и один альтернативный вариант. "
            "Они должны заметно отличаться."
        )

    system = """
Ты — персональный AI-стилист.

Твоя задача — подобрать реальную одежду
конкретному пользователю на основе его
гардероба и актуальной погоды.

КРИТИЧЕСКИ ВАЖНО:

1. Используй только вещи из списка ЕСТЬ.

2. Если вещь находится в списке ТОЧНО НЕТ,
никогда её не советуй.

3. Все вещи из ОБЯЗАТЕЛЬНО ИСПОЛЬЗОВАТЬ
должны присутствовать в основном образе.

4. Если обязательная вещь плохо подходит
по погоде, не убирай её.
Добавь подходящий слой поверх или под неё.

5. Учитывай цвет вещей.

6. Предпочтительные цвета используй чаще,
но не обязательно в каждой вещи.

7. Не повторяй один и тот же шаблон.
Меняй комбинации вещей.

8. Если есть возможность сделать два
разных образа из имеющегося гардероба —
делай их разными.

9. Учитывай стиль и место назначения.

10. Дождь определяй по всем переданным
погодным данным. Если хотя бы один источник
показывает реальные признаки дождя,
предупреди пользователя.

11. Не говори "дождя нет", если дождь уже идёт
или есть заметная вероятность дождя
в ближайшие часы.

12. Не придумывай погодные значения.

13. Не придумывай вещи.

14. Не используй Markdown-жирность.
Не используй символы **.

15. Не используй длинные вступления.

16. Пиши естественно и конкретно.

Ответ должен быть на русском.

Формат:

👕 ОСНОВНОЙ ОБРАЗ

Верх: ...
Низ: ...
Обувь: ...
Слой: ...
Аксессуары: ...

🌦 ПОГОДА

Короткое объяснение.

☔ ДОЖДЬ

Короткий практический совет.

🔄 АЛЬТЕРНАТИВА

Другой вариант одежды.

💡 СОВЕТ

Одна полезная рекомендация.

Не ставь ** вокруг текста.
"""

    user = f"""
ПРОФИЛЬ:

Город:
{display_city(profile)}

Стиль:
{profile.style}

Сценарий:
{profile.scenario}

Чувствительность к холоду:
{profile.cold_tolerance}

Тип одежды:
{profile.clothing}

ГАРДЕРОБ:

{wardrobe}

ПОГОДА:

{weather}

ДОПОЛНИТЕЛЬНАЯ ИНСТРУКЦИЯ:

{style_hint}

ЗАДАЧА:

{task}
"""

    try:

        response = (
            groq_client
            .chat
            .completions
            .create(

                model=GROQ_MODEL,

                messages=[
                    {
                        "role": "system",
                        "content": system,
                    },
                    {
                        "role": "user",
                        "content": user,
                    },
                ],

                temperature=0.75,

                max_tokens=800,

                # GPT-OSS поддерживает low.
                # reasoning выключаем в выводе,
                # чтобы пользователь не видел
                # лишние рассуждения.
                reasoning_effort="low",

                include_reasoning=False,
            )
        )

        result = (
            response
            .choices[0]
            .message
            .content
        )

        if result:

            return clean_ai_text(
                result
            )

    except Exception:

        log.exception(
            "Groq request failed"
        )

    # -----------------------------------------------------
    # Резервный ответ
    # -----------------------------------------------------

    current = snapshot[
        "current"
    ]

    temperature = float(
        current["feels"]
    )

    if temperature >= 25:

        base = (
            "футболку и лёгкий низ"
        )

    elif temperature >= 18:

        base = (
            "футболку и джинсы "
            "или лёгкие брюки"
        )

    elif temperature >= 10:

        base = (
            "футболку/лонгслив "
            "и лёгкую куртку"
        )

    elif temperature >= 3:

        base = (
            "лонгслив или свитер "
            "с курткой"
        )

    else:

        base = (
            "тёплые слои, куртку "
            "и закрытую обувь"
        )

    if snapshot["rain_now"]:

        rain_text = (
            "Сейчас есть признаки дождя. "
            "Зонт или непромокаемая верхняя "
            "одежда пригодятся."
        )

    elif snapshot["rain_soon"]:

        rain_text = (
            "В ближайшие часы возможны осадки. "
            "Лучше взять зонт."
        )

    else:

        rain_text = (
            "Заметных признаков дождя "
            "в ближайшие часы нет."
        )

    required_text = ""

    if profile.required:

        required_text = (
            "\n⭐ Обязательно используй: "
            + ", ".join(
                profile.required
            )
        )

    return (
        "👕 ОСНОВНОЙ ОБРАЗ\n\n"

        f"Базовый вариант: {base}.\n"

        f"{required_text}\n\n"

        "☔ ПОГОДА\n\n"

        f"{rain_text}\n\n"

        "💡 СОВЕТ\n\n"

        "Если станет теплее или холоднее, "
        "лучше использовать слой, который "
        "можно снять или добавить."
    )


# =========================================================
# START
# =========================================================

@dp.message(
    CommandStart()
)
async def start(
    message: Message,
    state: FSMContext,
):

    await state.clear()

    profile = get_profile(
        message.from_user.id
    )

    if not profile.city:

        await message.answer(
            "👋 Привет! Я Outfit AI.\n\n"

            "Я смотрю погоду и подбираю "
            "одежду с учётом твоего гардероба.\n\n"

            "Для начала напиши город.\n\n"

            "Например:\n"
            "Москва\n"
            "Сочи\n"
            "Казань"
        )

        await state.set_state(
            Setup.waiting_city
        )

        return

    await message.answer(
        "👋 С возвращением!\n\n"

        f"📍 {display_city(profile)}",

        reply_markup=main_keyboard(),
    )


# =========================================================
# CITY SETUP
# =========================================================

@dp.message(
    Setup.waiting_city
)
async def city_from_setup(
    message: Message,
    state: FSMContext,
):

    city = (
        message.text or ""
    ).strip()

    if len(city) < 2:

        await message.answer(
            "Напиши название города."
        )

        return

    profile = get_profile(
        message.from_user.id
    )

    try:

        ok = await load_and_update_city(
            profile,
            city,
        )

    except Exception:

        log.exception(
            "Geocoding failed"
        )

        ok = False

    if not ok:

        await message.answer(
            "❌ Не смог найти этот город.\n\n"

            "Попробуй написать точнее, "
            "например:\n"
            "Сочи, Россия"
        )

        return

    await state.clear()

    await message.answer(
        "📍 Город установлен.\n\n"

        f"{display_city(profile)}\n\n"

        "Теперь можно настроить гардероб "
        "или сразу спросить, что надеть.",

        reply_markup=main_keyboard(),
    )


# =========================================================
# HOME
# =========================================================

@dp.callback_query(
    F.data == "home"
)
async def home(
    callback: CallbackQuery,
):

    await callback.answer()

    profile = get_profile(
        callback.from_user.id
    )

    await callback.message.edit_text(
        "👕 Outfit AI\n\n"

        f"📍 {display_city(profile)}\n\n"

        "Что сделать?",

        reply_markup=main_keyboard(),
    )


# =========================================================
# PROFILE
# =========================================================

@dp.callback_query(
    F.data == "profile"
)
async def profile_menu(
    callback: CallbackQuery,
):

    await callback.answer()

    profile = get_profile(
        callback.from_user.id
    )

    await callback.message.edit_text(
        profile_text(profile),
        reply_markup=profile_keyboard(),
    )


# =========================================================
# CITY
# =========================================================

@dp.callback_query(
    F.data == "set_city"
)
async def set_city(
    callback: CallbackQuery,
    state: FSMContext,
):

    await callback.answer()

    await callback.message.edit_text(
        "📍 Напиши новый город.\n\n"

        "Например:\n"
        "Сочи\n"
        "Москва\n"
        "Казань"
    )

    await state.set_state(
        Setup.waiting_city
    )


# =========================================================
# WARDROBE
# =========================================================

@dp.callback_query(
    F.data == "wardrobe"
)
async def wardrobe_menu(
    callback: CallbackQuery,
):

    await callback.answer()

    profile = get_profile(
        callback.from_user.id
    )

    await callback.message.edit_text(
        wardrobe_text(profile),
        reply_markup=wardrobe_keyboard(
            profile
        ),
    )


@dp.callback_query(
    F.data == "wardrobe_add"
)
async def wardrobe_add(
    callback: CallbackQuery,
    state: FSMContext,
):

    await callback.answer()

    await callback.message.edit_text(
        "➕ Напиши вещь, которую у тебя есть.\n\n"

        "Например:\n"
        "футболка\n"
        "школьные джинсы\n"
        "белая рубашка\n"
        "чёрная куртка\n"
        "кроссовки"
    )

    await state.set_state(
        Setup.waiting_item
    )


@dp.message(
    Setup.waiting_item
)
async def wardrobe_item(
    message: Message,
    state: FSMContext,
):

    name = (
        message.text or ""
    ).strip()

    if len(name) < 2:

        await message.answer(
            "Напиши название вещи."
        )

        return

    await state.update_data(
        item_name=name
    )

    kb = InlineKeyboardBuilder()

    for label, color in COLOR_CHOICES:

        kb.button(
            text=label,
            callback_data=f"itemcolor:{color}",
        )

    kb.adjust(2)

    await message.answer(
        "🎨 Какого цвета эта вещь?",
        reply_markup=kb.as_markup(),
    )

    await state.set_state(
        Setup.waiting_item_color
    )


@dp.callback_query(
    F.data.startswith("itemcolor:")
)
async def wardrobe_item_color(
    callback: CallbackQuery,
    state: FSMContext,
):

    color = callback.data.split(
        ":",
        1,
    )[1]

    data = await state.get_data()

    name = data.get(
        "item_name"
    )

    if not name:

        await callback.answer(
            "Ошибка",
            show_alert=True,
        )

        return

    profile = get_profile(
        callback.from_user.id
    )

    profile.wardrobe.append(
        ClothingItem(
            name=name,
            color=color,
            category=category_from_item(
                name
            ),
        )
    )

    # Если раньше вещь была отмечена
    # как отсутствующая — убираем её оттуда.
    profile.unavailable = [
        x
        for x in profile.unavailable
        if x.casefold()
        != name.casefold()
    ]

    await state.clear()

    await callback.answer(
        "Вещь добавлена"
    )

    await callback.message.edit_text(
        f"✅ Добавлено:\n\n"
        f"{name} — {color}",
        reply_markup=wardrobe_keyboard(
            profile
        ),
    )


@dp.callback_query(
    F.data == "wardrobe_delete"
)
async def wardrobe_delete(
    callback: CallbackQuery,
):

    await callback.answer()

    profile = get_profile(
        callback.from_user.id
    )

    await callback.message.edit_text(
        "🗑 Выбери вещь для удаления:",
        reply_markup=wardrobe_delete_keyboard(
            profile
        ),
    )


@dp.callback_query(
    F.data.startswith("delitem:")
)
async def delete_item(
    callback: CallbackQuery,
):

    index = int(
        callback.data.split(
            ":",
            1,
        )[1]
    )

    profile = get_profile(
        callback.from_user.id
    )

    if (
        index < 0
        or index >= len(
            profile.wardrobe
        )
    ):

        await callback.answer(
            "Вещь уже удалена"
        )

        return

    item = profile.wardrobe.pop(
        index
    )

    profile.required = [
        x
        for x in profile.required
        if x.casefold()
        != item.name.casefold()
    ]

    await callback.answer(
        "Удалено"
    )

    await callback.message.edit_text(
        wardrobe_text(profile),
        reply_markup=wardrobe_keyboard(
            profile
        ),
    )


# =========================================================
# REQUIRED
# =========================================================

@dp.callback_query(
    F.data == "required"
)
async def required_menu(
    callback: CallbackQuery,
):

    await callback.answer()

    profile = get_profile(
        callback.from_user.id
    )

    if not profile.wardrobe:

        await callback.message.edit_text(
            "⭐ Сначала добавь вещи в гардероб.\n\n"

            "Например, добавь:\n"
            "школьные джинсы\n"
            "рубашку\n"
            "футболку",

            reply_markup=wardrobe_keyboard(
                profile
            ),
        )

        return

    await callback.message.edit_text(
        "⭐ Обязательные вещи\n\n"

        "Нажми на вещь, чтобы включить "
        "или выключить обязательность.\n\n"

        "Например, можно сделать "
        "«школьные джинсы» обязательными.",

        reply_markup=required_keyboard(
            profile
        ),
    )


@dp.callback_query(
    F.data.startswith("reqitem:")
)
async def toggle_required(
    callback: CallbackQuery,
):

    index = int(
        callback.data.split(
            ":",
            1,
        )[1]
    )

    profile = get_profile(
        callback.from_user.id
    )

    if (
        index < 0
        or index >= len(
            profile.wardrobe
        )
    ):

        await callback.answer(
            "Вещь не найдена"
        )

        return

    item = profile.wardrobe[
        index
    ]

    existing = next(
        (
            x
            for x in profile.required
            if x.casefold()
            == item.name.casefold()
        ),
        None,
    )

    if existing:

        profile.required = [
            x
            for x in profile.required
            if x.casefold()
            != item.name.casefold()
        ]

        await callback.answer(
            "Убрано из обязательных"
        )

    else:

        profile.required.append(
            item.name
        )

        await callback.answer(
            "Теперь обязательно"
        )

    await callback.message.edit_text(
        "⭐ Обязательные вещи\n\n"

        "Выбери вещи, которые AI "
        "обязан учитывать:",

        reply_markup=required_keyboard(
            profile
        ),
    )


# =========================================================
# MISSING
# =========================================================

@dp.callback_query(
    F.data == "unavailable"
)
async def unavailable_menu(
    callback: CallbackQuery,
):

    await callback.answer()

    profile = get_profile(
        callback.from_user.id
    )

    missing = (
        "\n".join(
            f"🚫 {x}"
            for x in profile.unavailable
        )
        if profile.unavailable
        else "Пока ничего не указано."
    )

    kb = InlineKeyboardBuilder()

    kb.button(
        text="➕ Добавить отсутствующую вещь",
        callback_data="missing_add",
    )

    if profile.unavailable:

        kb.button(
            text="🧹 Очистить список",
            callback_data="missing_clear",
        )

    kb.button(
        text="⬅️ Назад",
        callback_data="profile",
    )

    kb.adjust(1)

    await callback.message.edit_text(
        "🚫 ЧЕГО НЕТ\n\n"
        f"{missing}\n\n"

        "Это отдельный фильтр. "
        "AI никогда не должен советовать "
        "эти вещи.",

        reply_markup=kb.as_markup(),
    )


@dp.callback_query(
    F.data == "missing_add"
)
async def missing_add(
    callback: CallbackQuery,
    state: FSMContext,
):

    await callback.answer()

    await callback.message.edit_text(
        "🚫 Напиши вещь, которой у тебя нет.\n\n"

        "Например:\n"
        "пальто\n"
        "белые кеды\n"
        "чёрный свитер"
    )

    await state.set_state(
        Setup.waiting_missing_item
    )


@dp.message(
    Setup.waiting_missing_item
)
async def save_missing_item(
    message: Message,
    state: FSMContext,
):

    name = (
        message.text or ""
    ).strip()

    if len(name) < 2:

        await message.answer(
            "Напиши название вещи."
        )

        return

    profile = get_profile(
        message.from_user.id
    )

    if not any(
        x.casefold()
        == name.casefold()
        for x in profile.unavailable
    ):

        profile.unavailable.append(
            name
        )

    await state.clear()

    await message.answer(
        f"🚫 Добавлено в список отсутствующих:\n"
        f"{name}",

        reply_markup=profile_keyboard(),
    )


@dp.callback_query(
    F.data == "missing_clear"
)
async def missing_clear(
    callback: CallbackQuery,
):

    profile = get_profile(
        callback.from_user.id
    )

    profile.unavailable.clear()

    await callback.answer(
        "Список очищен"
    )

    await unavailable_menu(
        callback
    )


# =========================================================
# COLORS
# =========================================================

@dp.callback_query(
    F.data == "colors"
)
async def colors_menu(
    callback: CallbackQuery,
):

    await callback.answer()

    profile = get_profile(
        callback.from_user.id
    )

    selected = (
        ", ".join(
            profile.preferred_colors
        )
        if profile.preferred_colors
        else "нет"
    )

    await callback.message.edit_text(
        "🎨 ЛЮБИМЫЕ ЦВЕТА\n\n"

        f"Сейчас: {selected}\n\n"

        "Выбери цвета, которые AI "
        "будет стараться использовать "
        "в образах.",

        reply_markup=colors_keyboard(
            profile
        ),
    )


@dp.callback_query(
    F.data.startswith("prefcolor:")
)
async def toggle_color(
    callback: CallbackQuery,
):

    color = callback.data.split(
        ":",
        1,
    )[1]

    profile = get_profile(
        callback.from_user.id
    )

    if color in profile.preferred_colors:

        profile.preferred_colors.remove(
            color
        )

        await callback.answer(
            "Цвет убран"
        )

    else:

        profile.preferred_colors.append(
            color
        )

        await callback.answer(
            "Цвет добавлен"
        )

    await colors_menu(
        callback
    )


@dp.callback_query(
    F.data == "clearcolors"
)
async def clear_colors(
    callback: CallbackQuery,
):

    profile = get_profile(
        callback.from_user.id
    )

    profile.preferred_colors.clear()

    await callback.answer(
        "Цвета очищены"
    )

    await colors_menu(
        callback
    )


# =========================================================
# STYLE
# =========================================================

@dp.callback_query(
    F.data == "style"
)
async def style_menu(
    callback: CallbackQuery,
):

    await callback.answer()

    await callback.message.edit_text(
        "🎨 Какой стиль учитывать?",

        reply_markup=choice_keyboard(
            "style",
            [
                (
                    "👕 Повседневный",
                    "Повседневный",
                ),
                (
                    "🧢 Спортивный",
                    "Спортивный",
                ),
                (
                    "🖤 Минимализм",
                    "Минимализм",
                ),
                (
                    "✨ Нарядный",
                    "Нарядный",
                ),
                (
                    "🧥 Streetwear",
                    "Streetwear",
                ),
            ],
        ),
    )


@dp.callback_query(
    F.data.startswith("style:")
)
async def set_style(
    callback: CallbackQuery,
):

    profile = get_profile(
        callback.from_user.id
    )

    profile.style = (
        callback.data.split(
            ":",
            1,
        )[1]
    )

    await callback.answer(
        "Стиль сохранён"
    )

    await profile_menu(
        callback
    )


# =========================================================
# SCENARIO
# =========================================================

@dp.callback_query(
    F.data == "scenario"
)
async def scenario_menu(
    callback: CallbackQuery,
):

    await callback.answer()

    await callback.message.edit_text(
        "🎯 Куда ты сегодня идёшь?",

        reply_markup=choice_keyboard(
            "scenario",
            [
                (
                    "🚶 Прогулка",
                    "Прогулка",
                ),
                (
                    "🏫 Школа / учёба",
                    "Школа/учёба",
                ),
                (
                    "💼 Работа",
                    "Работа",
                ),
                (
                    "🏃 Спорт",
                    "Спорт",
                ),
                (
                    "🎉 Вечеринка",
                    "Вечеринка",
                ),
                (
                    "🏠 Просто выйти",
                    "Просто выйти",
                ),
            ],
        ),
    )


@dp.callback_query(
    F.data.startswith("scenario:")
)
async def set_scenario(
    callback: CallbackQuery,
):

    profile = get_profile(
        callback.from_user.id
    )

    profile.scenario = (
        callback.data.split(
            ":",
            1,
        )[1]
    )

    await callback.answer(
        "Сохранено"
    )

    await profile_menu(
        callback
    )


# =========================================================
# COLD
# =========================================================

@dp.callback_query(
    F.data == "cold"
)
async def cold_menu(
    callback: CallbackQuery,
):

    await callback.answer()

    await callback.message.edit_text(
        "🥶 Как ты переносишь холод?",

        reply_markup=choice_keyboard(
            "cold",
            [
                (
                    "🥶 Мёрзну быстро",
                    "Мёрзну быстро",
                ),
                (
                    "🙂 Нормально",
                    "Нормально переношу холод",
                ),
                (
                    "🔥 Почти не мёрзну",
                    "Почти не мёрзну",
                ),
            ],
        ),
    )


@dp.callback_query(
    F.data.startswith("cold:")
)
async def set_cold(
    callback: CallbackQuery,
):

    profile = get_profile(
        callback.from_user.id
    )

    profile.cold_tolerance = (
        callback.data.split(
            ":",
            1,
        )[1]
    )

    await callback.answer(
        "Сохранено"
    )

    await profile_menu(
        callback
    )


# =========================================================
# CLOTHING TYPE
# =========================================================

@dp.callback_query(
    F.data == "clothing"
)
async def clothing_menu(
    callback: CallbackQuery,
):

    await callback.answer()

    await callback.message.edit_text(
        "👔 Какой тип одежды учитывать?",

        reply_markup=choice_keyboard(
            "clothing",
            [
                (
                    "👨 Мужская",
                    "Мужская",
                ),
                (
                    "👩 Женская",
                    "Женская",
                ),
                (
                    "🧑 Универсальная",
                    "Универсальная",
                ),
            ],
        ),
    )


@dp.callback_query(
    F.data.startswith("clothing:")
)
async def set_clothing(
    callback: CallbackQuery,
):

    profile = get_profile(
        callback.from_user.id
    )

    profile.clothing = (
        callback.data.split(
            ":",
            1,
        )[1]
    )

    await callback.answer(
        "Сохранено"
    )

    await profile_menu(
        callback
    )


# =========================================================
# WEATHER
# =========================================================

async def get_complete_weather(
    profile: UserProfile,
) -> dict:

    # Основной источник
    open_meteo = await get_open_meteo(
        profile
    )

    # Дополнительный источник.
    # Если он недоступен — основной
    # источник всё равно работает.
    secondary = await get_wttr(
        profile
    )

    return build_weather_snapshot(
        open_meteo,
        profile,
        secondary,
    )


async def send_weather(
    message: Message,
    profile: UserProfile,
):

    if not profile.city:

        await message.answer(
            "Сначала установи город.",
            reply_markup=main_keyboard(),
        )

        return

    try:

        snapshot = (
            await get_complete_weather(
                profile
            )
        )

    except Exception:

        log.exception(
            "Weather request failed"
        )

        await message.answer(
            "❌ Не удалось получить погоду.\n"
            "Попробуй ещё раз через несколько секунд.",

            reply_markup=main_keyboard(),
        )

        return

    await message.answer(
        format_weather_message(
            profile,
            snapshot,
        ),

        reply_markup=main_keyboard(),
    )


async def send_outfit(
    message: Message,
    profile: UserProfile,
    request_type: str,
):

    if not profile.city:

        await message.answer(
            "Сначала установи город.",
            reply_markup=main_keyboard(),
        )

        return

    status = await message.answer(
        "🌤 Проверяю погоду и гардероб…"
    )

    try:

        snapshot = (
            await get_complete_weather(
                profile
            )
        )

        answer = await ask_groq(
            profile,
            snapshot,
            request_type,
        )

    except Exception:

        log.exception(
            "Outfit flow failed"
        )

        await status.edit_text(
            "❌ Не получилось получить совет.\n"
            "Попробуй ещё раз.",

            reply_markup=main_keyboard(),
        )

        return

    await status.edit_text(
        clean_ai_text(answer),
        reply_markup=main_keyboard(),
    )


# =========================================================
# CALLBACKS
# =========================================================

@dp.callback_query(
    F.data == "weather"
)
async def weather_callback(
    callback: CallbackQuery,
):

    await callback.answer()

    await send_weather(
        callback.message,
        get_profile(
            callback.from_user.id
        ),
    )


@dp.callback_query(
    F.data == "outfit"
)
async def outfit_callback(
    callback: CallbackQuery,
):

    await callback.answer()

    await send_outfit(
        callback.message,
        get_profile(
            callback.from_user.id
        ),
        "outfit",
    )


@dp.callback_query(
    F.data == "day"
)
async def day_callback(
    callback: CallbackQuery,
):

    await callback.answer()

    await send_outfit(
        callback.message,
        get_profile(
            callback.from_user.id
        ),
        "day",
    )


# =========================================================
# COMMANDS
# =========================================================

@dp.message(
    Command("city")
)
async def city_command(
    message: Message,
    state: FSMContext,
):

    await message.answer(
        "📍 Напиши город, который установить."
    )

    await state.set_state(
        Setup.waiting_city
    )


@dp.message(
    Command("weather")
)
async def weather_command(
    message: Message,
):

    await send_weather(
        message,
        get_profile(
            message.from_user.id
        ),
    )


@dp.message(
    Command("outfit")
)
async def outfit_command(
    message: Message,
):

    await send_outfit(
        message,
        get_profile(
            message.from_user.id
        ),
        "outfit",
    )


# =========================================================
# TEXT
# =========================================================

@dp.message()
async def text_handler(
    message: Message,
):

    text = (
        message.text or ""
    ).strip().lower()

    profile = get_profile(
        message.from_user.id
    )

    if any(
        word in text
        for word in (
            "что надеть",
            "как одеться",
            "одеться",
            "наряд",
            "образ",
        )
    ):

        await send_outfit(
            message,
            profile,
            "outfit",
        )

        return

    if "погода" in text:

        await send_weather(
            message,
            profile,
        )

        return

    await message.answer(
        "Я могу подобрать одежду "
        "по реальной погоде и твоему гардеробу.\n\n"

        "Выбери действие:",

        reply_markup=main_keyboard(),
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    bot = Bot(
        token=BOT_TOKEN,

        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    try:

        me = await bot.get_me()

        log.info(
            "Bot started: @%s",
            me.username,
        )

        await bot.delete_webhook(
            drop_pending_updates=True
        )

        await dp.start_polling(
            bot
        )

    finally:

        await bot.session.close()


if __name__ == "__main__":

    asyncio.run(main())
