# Outfit AI Telegram Bot v2

AI-бот-стилист для Telegram.

Бот получает актуальную погоду через Open-Meteo
и использует Groq для составления рекомендации,
что надеть.

## Возможности

- актуальная температура;
- ощущаемая температура;
- дождь;
- снег;
- вероятность осадков;
- ветер;
- порывы ветра;
- влажность;
- UV index;
- прогноз по ближайшим часам;
- совет «Что надеть?»;
- совет на весь день;
- профиль пользователя;
- город;
- стиль;
- сценарий;
- чувствительность к холоду;
- тип одежды;
- резервный совет при временной ошибке Groq.

## Установка

Создай Telegram-бота через @BotFather.

Получи Groq API key.

Загрузи файлы проекта в GitHub:

- main.py
- requirements.txt
- .env.example
- .gitignore
- README.md

## Railway

Подключи GitHub repository к Railway.

В Railway → Variables добавь:

BOT_TOKEN=токен Telegram
GROQ_API_KEY=ключ Groq
GROQ_MODEL=openai/gpt-oss-120b

Start Command:

python main.py

## Важно

Не добавляй настоящий .env в GitHub.

Railway Volume не нужен.

База данных не нужна.

Профиль пользователя в этой версии хранится
в оперативной памяти и сбрасывается после
перезапуска контейнера.

## Команды

/start
/city
/weather
/outfit
