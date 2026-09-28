import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
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

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("outfit-bot")

groq_client = Groq(api_key=GROQ_API_KEY)

dp = Dispatcher(storage=MemoryStorage())


@dataclass
class UserProfile:
    city: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: str = "Europe/Moscow"
    country: Optional[str] = None

    style: str = "Повседневный"
    scenario: str = "Прогулка"
    cold_tolerance: str = "Нормально переношу холод"
    clothing: str = "Универсальная"

    updated_at: Optional[str] = None


profiles: dict[int, UserProfile] = {}


class Setup(StatesGroup):
    waiting_city = State()


def get_profile(user_id: int) -> UserProfile:
    if user_id not in profiles:
        profiles[user_id] = UserProfile()

    return profiles[user_id]


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


async def http_json(
    url: str,
    params: dict,
    timeout: int = 12,
) -> dict:

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)

    async with aiohttp.ClientSession(
        timeout=timeout_cfg
    ) as session:

        async with session.get(
            url,
            params=params,
        ) as response:

            response.raise_for_status()

            return await response.json()


async def geocode_city(city: str) -> Optional[dict]:

    data = await http_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        {
            "name": city,
            "count": 5,
            "language": "ru",
            "format": "json",
        },
    )

    results = data.get("results") or []

    if not results:
        return None

    city_clean = city.strip().casefold()

    exact = next(
        (
            result
            for result in results
            if str(
                result.get("name", "")
            ).casefold() == city_clean
        ),
        results[0],
    )

    return exact


async def get_weather(
    profile: UserProfile,
) -> dict:

    if (
        profile.latitude is None
        or profile.longitude is None
    ):
        raise ValueError("Город не настроен")

    data = await http_json(
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

    return data


WEATHER_CODES = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь/туман",
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
    80: "слабые ливни",
    81: "ливни",
    82: "сильные ливни",
    85: "слабый снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}


def weather_text(code: int) -> str:
    return WEATHER_CODES.get(
        int(code),
        "неизвестные условия",
    )


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

    hours = data["hourly"]["time"]

    if not hours:
        return 0

    current = now.replace(tzinfo=None)

    best = 0
    best_diff = float("inf")

    for i, value in enumerate(hours):

        try:
            dt = datetime.fromisoformat(value)

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
        len(hourly["time"]),
    )

    next_hours = []

    for j in range(i, end):

        next_hours.append(
            {
                "time": hourly["time"][j],
                "temp": hourly["temperature_2m"][j],
                "feels": hourly["apparent_temperature"][j],
                "rain_probability": hourly[
                    "precipitation_probability"
                ][j],
                "precipitation": hourly[
                    "precipitation"
                ][j],
                "rain": hourly["rain"][j],
                "snow": hourly["snowfall"][j],
                "wind": hourly["wind_speed_10m"][j],
                "gust": hourly["wind_gusts_10m"][j],
                "code": hourly["weather_code"][j],
            }
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
            "rain": current.get("rain"),
            "snow": current.get("snowfall"),
            "wind": current.get(
                "wind_speed_10m"
            ),
            "gust": current.get(
                "wind_gusts_10m"
            ),
            "humidity": current.get(
                "relative_humidity_2m"
            ),
            "code": current.get(
                "weather_code"
            ),
            "description": weather_text(
                current.get(
                    "weather_code",
                    0,
                )
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
    }


def compact_weather_text(
    snapshot: dict,
) -> str:

    current = snapshot["current"]
    today = snapshot["today"]

    lines = [
        (
            f"Сейчас: {current['temperature']}°C, "
            f"ощущается как {current['feels']}°C, "
            f"{current['description']}."
        ),

        (
            f"Ветер: {current['wind']} км/ч, "
            f"порывы до {current['gust']} км/ч."
        ),

        (
            f"Влажность: "
            f"{current['humidity']}%."
        ),

        (
            f"Осадки сейчас: "
            f"{current['precipitation']} мм, "
            f"дождь {current['rain']} мм, "
            f"снег {current['snow']} мм."
        ),

        (
            f"Сегодня: "
            f"{today['min']}…{today['max']}°C, "
            f"ощущается "
            f"{today['feels_min']}…"
            f"{today['feels_max']}°C, "
            f"вероятность осадков "
            f"до {today['precip_probability']}%, "
            f"макс. ветер "
            f"{today['wind_max']} км/ч, "
            f"порывы "
            f"{today['gust_max']} км/ч, "
            f"UV до {today['uv']}."
        ),

        "Ближайшие часы:",
    ]

    for hour in snapshot["next_hours"]:

        lines.append(
            f"{hour['time'][11:16]} — "
            f"{hour['temp']}°C, "
            f"ощущ. {hour['feels']}°C, "
            f"осадки "
            f"{hour['rain_probability']}%, "
            f"{weather_text(hour['code'])}, "
            f"ветер "
            f"{hour['wind']} км/ч."
        )

    return "\n".join(lines)


def format_weather_message(
    profile: UserProfile,
    snapshot: dict,
) -> str:

    current = snapshot["current"]
    today = snapshot["today"]

    rain_probability = today[
        "precip_probability"
    ]

    if rain_probability >= 40:
        rain_emoji = "☔"
    elif rain_probability >= 15:
        rain_emoji = "🌂"
    else:
        rain_emoji = "☀️"

    return (
        f"🌤 <b>{profile.city}</b>\n\n"

        f"🌡 Сейчас: "
        f"<b>{current['temperature']}°C</b>\n"

        f"🥶 Ощущается: "
        f"<b>{current['feels']}°C</b>\n"

        f"☁️ "
        f"{current['description'].capitalize()}\n"

        f"💨 Ветер: "
        f"<b>{current['wind']} км/ч</b>, "
        f"порывы до "
        f"{current['gust']} км/ч\n"

        f"💧 Влажность: "
        f"<b>{current['humidity']}%</b>\n"

        f"{rain_emoji} Вероятность осадков сегодня: "
        f"<b>{rain_probability}%</b>\n"

        f"🌡 Диапазон дня: "
        f"<b>{today['min']}…"
        f"{today['max']}°C</b>\n"

        f"☀️ UV: "
        f"<b>{today['uv']}</b>"
    )


def profile_text(
    profile: UserProfile,
) -> str:

    city = (
        profile.city
        or "не установлен"
    )

    return (
        "⚙️ <b>Твой профиль</b>\n\n"

        f"📍 Город: "
        f"<b>{city}</b>\n"

        f"🎨 Стиль: "
        f"<b>{profile.style}</b>\n"

        f"🎯 Куда: "
        f"<b>{profile.scenario}</b>\n"

        f"🥶 Холод: "
        f"<b>{profile.cold_tolerance}</b>\n"

        f"👔 Одежда: "
        f"<b>{profile.clothing}</b>"
    )


async def ask_groq(
    profile: UserProfile,
    snapshot: dict,
    request_type: str = "outfit",
) -> str:

    weather = compact_weather_text(
        snapshot
    )

    if request_type == "day":

        task = (
            "Составь план одежды на день "
            "с учётом того, что погода может "
            "меняться. Укажи базовый комплект "
            "утром, что добавить или снять "
            "днём и что взять с собой."
        )

    else:

        task = (
            "Дай конкретный совет, что человеку "
            "надеть прямо сейчас и что взять "
            "с собой. Если в ближайшие часы "
            "ожидается дождь или снег, обязательно "
            "предупреди об этом."
        )

    system = """
Ты — практичный AI-стилист, который советует одежду по реальной погоде.

Отвечай по-русски.

Не придумывай погодные данные.
Используй только переданные значения.

Главная цель — конкретный полезный совет,
а не длинная лекция.

Учитывай:
- стиль пользователя;
- сценарий;
- чувствительность к холоду;
- тип одежды;
- температуру;
- ощущаемую температуру;
- дождь;
- снег;
- ветер;
- ближайшие часы.

Если погода пограничная,
предложи слой, который можно снять.

Не называй бренды без просьбы.

Формат:

👕 <b>Что надеть:</b>
- верх:
- низ:
- обувь:
- верхняя одежда/слой:
- аксессуары:

🌦 <b>Погода:</b>
1–2 коротких предложения.

💡 <b>Совет:</b>
1–2 практичных предложения.

Не используй оценочные шкалы.
Не придумывай температуру.
"""

    user = f"""
Профиль пользователя:

Город: {profile.city}
Стиль: {profile.style}
Куда идёт: {profile.scenario}
Чувствительность к холоду: {profile.cold_tolerance}
Тип одежды: {profile.clothing}

Реальные данные погоды:

{weather}

Задача:

{task}
"""

    try:

        response = groq_client.chat.completions.create(
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

            temperature=0.35,
            max_tokens=700,
            reasoning_effort="low",
        )

        result = (
            response
            .choices[0]
            .message
            .content
        )

        if result:
            return result.strip()

    except Exception:

        log.exception(
            "Groq request failed"
        )

    # Резервный совет,
    # если Groq временно недоступен.

    current = snapshot["current"]

    temperature = float(
        current["feels"]
    )

    if temperature >= 25:

        base = (
            "лёгкую футболку "
            "и шорты/лёгкие брюки"
        )

    elif temperature >= 18:

        base = (
            "футболку "
            "и лёгкие брюки или джинсы"
        )

    elif temperature >= 10:

        base = (
            "футболку/лонгслив "
            "и лёгкую куртку"
        )

    elif temperature >= 3:

        base = (
            "слои: лонгслив или свитер "
            "+ куртку"
        )

    else:

        base = (
            "тёплые слои, куртку "
            "и закрытую обувь"
        )

    rain = snapshot[
        "today"
    ]["precip_probability"]

    if rain >= 40:

        umbrella = (
            "☔ Зонт лучше взять."
        )

    else:

        umbrella = (
            "🌂 Зонт, скорее всего, "
            "не понадобится."
        )

    return (
        "👕 <b>Что надеть:</b>\n"

        f"- базовый комплект: "
        f"{base}\n"

        "- обувь: удобная закрытая "
        "обувь по ситуации\n\n"

        "🌦 <b>Погода:</b>\n"

        f"Ощущается как "
        f"{current['feels']}°C, "
        f"{current['description']}. "

        f"Вероятность осадков сегодня — "
        f"{rain}%.\n\n"

        f"💡 {umbrella}"
    )


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

    profile.country = result.get(
        "country"
    )

    profile.updated_at = (
        datetime.utcnow().isoformat()
    )

    return True


@dp.message(CommandStart())
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
            "👋 <b>Привет! Я Outfit AI</b>\n\n"

            "Я смотрю актуальную погоду "
            "и помогаю решить, что надеть.\n\n"

            "Для начала напиши свой город — например:\n"

            "<code>Москва</code>"
        )

        await state.set_state(
            Setup.waiting_city
        )

        return

    await message.answer(
        "👋 С возвращением!\n\n"
        f"📍 Сейчас выбран город: "
        f"<b>{profile.city}</b>",

        reply_markup=main_keyboard(),
    )


@dp.message(Setup.waiting_city)
async def city_from_setup(
    message: Message,
    state: FSMContext,
):

    city = (
        message.text or ""
    ).strip()

    if len(city) < 2:

        await message.answer(
            "Напиши название города, "
            "например: <b>Москва</b>."
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
            "например: "
            "<b>Москва, Россия</b>."
        )

        return

    await state.clear()

    await message.answer(
        f"📍 Город установлен: "
        f"<b>{profile.city}</b>\n\n"

        "Теперь я могу подобрать "
        "одежду по погоде.",

        reply_markup=main_keyboard(),
    )


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
        "👕 <b>Outfit AI</b>\n\n"

        f"📍 Город: "
        f"<b>{profile.city or 'не установлен'}</b>\n\n"

        "Что сделать?",

        reply_markup=main_keyboard(),
    )


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
        "Например: <b>Москва</b>"
    )

    await state.set_state(
        Setup.waiting_city
    )


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

    await callback.answer(
        "Сохранено"
    )

    get_profile(
        callback.from_user.id
    ).style = (
        callback.data.split(
            ":",
            1,
        )[1]
    )

    await profile_menu(
        callback
    )


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
                    "🏫 Школа/учёба",
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

    await callback.answer(
        "Сохранено"
    )

    get_profile(
        callback.from_user.id
    ).scenario = (
        callback.data.split(
            ":",
            1,
        )[1]
    )

    await profile_menu(
        callback
    )


@dp.callback_query(
    F.data == "cold"
)
async def cold_menu(
    callback: CallbackQuery,
):

    await callback.answer()

    await callback.message.edit_text(
        "🥶 Как ты обычно переносишь холод?",

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

    await callback.answer(
        "Сохранено"
    )

    get_profile(
        callback.from_user.id
    ).cold_tolerance = (
        callback.data.split(
            ":",
            1,
        )[1]
    )

    await profile_menu(
        callback
    )


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

    await callback.answer(
        "Сохранено"
    )

    get_profile(
        callback.from_user.id
    ).clothing = (
        callback.data.split(
            ":",
            1,
        )[1]
    )

    await profile_menu(
        callback
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

        data = await get_weather(
            profile
        )

        snapshot = build_weather_snapshot(
            data,
            profile,
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
        "🌤 Смотрю погоду…"
    )

    try:

        data = await get_weather(
            profile
        )

        snapshot = build_weather_snapshot(
            data,
            profile,
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
        answer,
        reply_markup=main_keyboard(),
    )


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
        "Я могу помочь выбрать одежду по погоде.\n\n"
        "Выбери действие:",

        reply_markup=main_keyboard(),
    )


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
