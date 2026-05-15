# TManager

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)
![LM Studio](https://img.shields.io/badge/LLM-LM%20Studio-purple)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)
![Offline](https://img.shields.io/badge/works-100%25%20offline-brightgreen)

**Telegram-бот для задач и продуктивности. Работает полностью локально — никаких подписок, никакого облака.**

LLM крутится у тебя на компьютере через LM Studio, голос распознаётся через faster-whisper. Начинал как простой трекер задач, сейчас умеет составлять расписания через ИИ, показывать графики продуктивности, ставить помодоро-таймеры и показывать погоду.

---

## Что умеет

**Задачи**
- Добавлять задачи с дедлайном, временем и приоритетом
- Отмечать выполненными (или «запретить выполнение» — для задач-привычек, которые повторяются каждый день)
- Отслеживать streak: 3 дня подряд — 🌿, 7 — 🔥, 30 — 💎
- Напоминать перед задачей если указано время

**Расписание**
- ИИ составляет недельный план по твоему описанию
- Можно редактировать голосом или текстом: «перенеси зарядку с пятницы на четверг»
- Большие изменения откатываются кнопкой ↩

**Аналитика**
- `/today` — сколько задач выполнено сегодня, AI комментирует
- `/week` — диаграмма по дням недели
- `/month` — календарная сетка месяца с цветными квадратами (как на GitHub)

**Помодоро**
- `/pomodoro` — запускает 25-минутный таймер, после предлагает перерыв
- `/pomodoro 45` — если хочешь подольше
- `/pomodoro stop` — остановить

**Погода**
- `/weather` — текущая погода по твоему городу (через Яндекс.Погоду)
- Погода добавляется в утреннее уведомление автоматически, если задан город

**Заметки**
- `/note <текст>` — сохранить заметку
- Или просто напиши «запомни, что...» — бот сам разберётся

**Голос**
- Отправь голосовое — faster-whisper расшифрует, бот добавит задачи или ответит

**Характер бота**
- В настройках можно переключить: мягкий, нейтральный, требовательный, игривый
- ИИ ведёт дневник наблюдений о тебе и учитывает его в разговорах

**Уведомления**
- Утренний дайджест с погодой и планом на день
- Вечерний итог
- Напоминания за N минут до задачи (выбираешь сам: 10/15/30/60/90/120 мин)

---

## Команды

```
/start         — создать аккаунт, приветствие
/menu          — главное меню с кнопками
/task <текст>  — добавить задачу
/task Зарядка | daily          — ежедневная привычка
/task Отчёт | 2025-12-31       — с дедлайном
/tasks         — список активных задач
/done <id>     — выполнено
/overdue       — просроченные
/schedule      — составить расписание (ИИ уточнит детали)
/myplan        — посмотреть расписание на эту неделю
/today         — итог дня
/week          — диаграмма недели
/month         — календарь месяца
/pomodoro      — запустить помодоро (25 мин)
/weather       — текущая погода
/note <текст>  — сохранить заметку
/streak        — серии выполненных задач
/settings      — характер бота, уведомления, город
```

Или просто пиши текстом — ИИ разберётся.

---

## Структура проекта

```
bot.py                     — точка входа
config.py                  — конфиг из .env
database.py                — SQLite через aiosqlite
ai_client.py               — клиент к LM Studio
voice_handler.py           — faster-whisper
scheduler_jobs.py          — утро/вечер/напоминания (APScheduler)
analytics.py               — matplotlib: графики today/week/month
api.py                     — REST API на Flask (для веб-интерфейса)
handlers/
  start_handler.py         — /start /help
  menu_handler.py          — кнопочное меню
  task_handler.py          — задачи, стрики, напоминания
  schedule_handler.py      — расписание
  analytics_handler.py     — /today /week /month
  notes_handler.py         — заметки
  pomodoro_handler.py      — таймер
  weather_handler.py       — погода
  voice_message_handler.py — голосовые
  ai_chat_handler.py       — свободный диалог
  settings_handler.py      — настройки
  admin_handler.py         — /admin (только для владельца)
```

---

# Установка (Windows 10/11)

Минут 30-60, большую часть времени займёт скачивание модели.

---

## 1. Python

1. Скачай Python 3.12 с **python.org/downloads**
2. При установке обязательно поставь галочку **«Add Python to PATH»**
3. Проверь: открой `cmd` и напиши `python --version`

---

## 2. LM Studio

LM Studio — это программа, которая запускает языковые модели локально. У неё встроенный сервер, совместимый с OpenAI API, поэтому бот просто к нему подключается.

1. Скачай с **lmstudio.ai**, установи
2. На вкладке **Discover** найди и скачай модель. Хорошо работают:
   - **Qwen3-8B** — быстрая, ~8 ГБ памяти, хорошо понимает русский
   - **Llama 3.1 8B Instruct** — тоже ~8 ГБ
   - Бери GGUF с квантизацией **Q4_K_M** — баланс скорости и качества
3. Перейди на вкладку **Developer** (значок `</>`)
4. Нажми **Start Server** — должно написать `Server running on http://localhost:4321`
5. Запомни точное имя модели — оно нужно для `.env`

> Если ответы медленные — это нормально на CPU. Модели 7-8B с Q4_K_M терпимо работают даже без видеокарты.

---

## 3. Скачать код

Просто положи файлы проекта в удобную папку, например:
```
C:\Users\ИМЯ\Documents\TManager
```

---

## 4. Виртуальное окружение

Открой `cmd`, перейди в папку проекта:
```
python -m venv venv
venv\Scripts\activate
```

В начале строки должно появиться `(venv)`. Без этого дальше не идти.

> Если в PowerShell не активируется — используй обычный `cmd`. Или в PowerShell выполни:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

---

## 5. Зависимости

```
pip install -r requirements.txt
```

Поставит: python-telegram-bot, openai (как HTTP-клиент к LM Studio), faster-whisper, matplotlib, aiosqlite, APScheduler, Flask, httpx, python-dotenv.

> Если ругается на `av` (PyAV) — скачай **Microsoft Visual C++ Redistributable** и попробуй снова.

---

## 6. Токен Telegram

1. Найди в Telegram **@BotFather**
2. Напиши `/newbot`, придумай имя и username (должен заканчиваться на `bot`)
3. Получишь токен вида `7123456789:AAHxxxxxxxx...`

---

## 7. Файл .env

Скопируй шаблон:
```
copy .env.example .env
```

Открой `.env` и заполни:

```env
TELEGRAM_TOKEN=7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxx

LMSTUDIO_BASE_URL=http://localhost:4321/v1
LMSTUDIO_MODEL=qwen/qwen3-8b        # точное имя из LM Studio

WHISPER_MODEL_SIZE=small             # tiny / base / small / medium / large-v3
WHISPER_DEVICE=cpu                   # cuda — если есть NVIDIA GPU
WHISPER_COMPUTE_TYPE=int8

DATABASE_PATH=tmanager.db
BOT_NAME=TManager
MORNING_MESSAGE_TIME=08:00
EVENING_REVIEW_TIME=21:00

# Яндекс.Погода (бесплатный тестовый ключ на 50 запросов/день):
# YANDEX_WEATHER_KEY=твой_ключ

# Твой Telegram ID (открывает /admin). Можно не заполнять.
# ADMIN_TELEGRAM_ID=123456789
```

Без кавычек, без пробелов вокруг `=`. Файл `.env` никому не показывай — там токен бота.

---

## 8. Запуск

Сначала убедись, что в LM Studio нажата кнопка **Start Server** и модель загружена. Потом:

```
python bot.py
```

Лог при нормальном старте:
```
[INFO] database: Database initialized at tmanager.db
[INFO] scheduler_jobs: Scheduler set up: morning=08:00, evening=21:00
[INFO] bot: TManager starting…
```

Открой Telegram, найди своего бота, напиши `/start`.

При первом голосовом сообщении faster-whisper скачает модель Whisper с Hugging Face (~500 МБ для `small`) — подожди немного, потом будет работать оффлайн.

Остановить — **Ctrl+C**.

---

## 9. Автозапуск (по желанию)

Создай файл `start_tmanager.bat` рядом с `bot.py`:
```bat
@echo off
cd /d C:\Users\ИМЯ\Documents\TManager
call venv\Scripts\activate
python bot.py
```

Нажми **Win+R**, введи `shell:startup`, скопируй туда этот файл.

LM Studio тоже нужно запускать вместе с Windows — в его настройках есть
*Settings → Developer → Auto-start server on launch*, плюс добавь его в автозагрузку Windows.

---

## Решение проблем

**`ConnectionRefusedError` при работе с ИИ**
→ В LM Studio нажата кнопка Start Server?
→ Порт в `.env` совпадает с тем, что показывает LM Studio? По умолчанию `4321`.

**Бот говорит, что модель не найдена**
→ Имя в `LMSTUDIO_MODEL` должно совпадать символ в символ с тем, что показывает LM Studio. Можно проверить через `curl http://localhost:4321/v1/models`.

**ИИ отвечает нормально, но расписание не парсится**
→ Модели поменьше иногда не следуют формату JSON. Попробуй Qwen3 или Llama 3.1 — они лучше с инструкциями.

**Голосовые не работают / ошибка PyAV**
→ Поставь Microsoft Visual C++ Redistributable.
→ Первое голосовое идёт медленно — Whisper скачивается. Это один раз.

**Медленные ответы**
→ Это CPU, так и должно быть. Помогает: модель поменьше (3-4B), квантизация Q4_0, или GPU-оффлоадинг в настройках LM Studio.

**`ModuleNotFoundError`**
→ Виртуальное окружение активно? Должно быть `(venv)` в начале строки. Если нет — `venv\Scripts\activate`.
