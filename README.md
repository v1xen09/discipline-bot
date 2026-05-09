# TManager 📋

Telegram-бот для управления задачами и продуктивностью с **локальным ИИ**.
Никаких облачных API — вся LLM работает у тебя на компьютере через **LM Studio**,
голос распознаётся локально через **faster-whisper**.

Составляет недельные расписания, распознаёт голосовые сообщения, отслеживает
серии выполненных дел, напоминает о просроченных задачах и мотивирует — с учётом
истории пользователя.

---

## Возможности

| Функция | Описание |
|---|---|
| 📅 Расписание | Локальная LLM составляет недельный план по словесному описанию |
| 🎤 Голосовые | faster-whisper расшифровывает — бот добавляет задачи или составляет план |
| 🔥 Серии | Отслеживает streak для каждой задачи, поздравляет на milestone'ах |
| ⏰ Напоминания | Утренний и вечерний дайджест, напоминания о дедлайнах |
| ⚠️ Просроченные | Проверка и мягкие напоминания о невыполненных делах |
| 🧠 Память | ИИ ведёт дневник наблюдений о пользователе и использует его в ответах |

---

## Команды бота

```
/start         — регистрация и приветствие
/help          — справка по командам
/task <текст>  — добавить задачу
/task Зарядка | daily       — ежедневная привычка
/task Отчёт | 2025-12-31    — задача с дедлайном
/tasks         — список активных задач
/done <id>     — отметить задачу выполненной
/overdue       — просроченные задачи
/schedule      — составить расписание на неделю
/myplan        — показать текущее расписание
/streak        — серии выполненных задач
```

Также можно просто написать боту текстом — он ответит с учётом твоего прогресса.
Или отправить голосовое сообщение — бот распознает и извлечёт задачи.

---

## Архитектура

```
bot.py                       — точка входа, регистрация хендлеров
config.py                    — конфигурация (.env)
database.py                  — SQLite: users, tasks, schedules, streaks, diary
ai_client.py                 — клиент LM Studio: расписание, мотивация, память
voice_handler.py             — faster-whisper: голос → текст
scheduler_jobs.py            — APScheduler: утро/вечер/reminders
handlers/
  start_handler.py           — /start /help
  schedule_handler.py        — /schedule /myplan
  task_handler.py            — /task /tasks /done /overdue /streak
  voice_message_handler.py   — голосовые сообщения
  ai_chat_handler.py         — свободный диалог
```

---

# Установка и запуск на Windows 10/11

> Весь процесс занимает 30–60 минут — основное время уходит на скачивание модели LM Studio.

---

## Шаг 1 — Установка Python

1. Открой **https://www.python.org/downloads/**
2. Нажми кнопку **«Download Python 3.12.x»**
3. Запусти скачанный установщик
4. **ВАЖНО:** на первом экране поставь галочку **«Add Python to PATH»**
5. Нажми **«Install Now»** и дождись завершения

**Проверка:** открой `cmd` и введи:
```
python --version
```
Должно появиться `Python 3.12.x`.

---

## Шаг 2 — Установка LM Studio

LM Studio — бесплатное приложение, которое запускает большие языковые модели
прямо на твоём компьютере. У него есть встроенный сервер с тем же API,
что у OpenAI, поэтому код бота работает без изменений.

1. Скачай LM Studio с **https://lmstudio.ai/**
2. Установи и запусти
3. На вкладке **Discover** (лупа в левом меню) найди и скачай модель.
   Хорошие варианты для русского языка и инструкций:
   - **Qwen3-8B** (`qwen/qwen3-8b`) — быстрая, нужно ≈8 ГБ памяти
   - **Llama 3.1 8B Instruct** — нужно ≈8 ГБ памяти
   - **Mistral Small** — нужно ≈14 ГБ памяти
   Выбирай GGUF-версию с квантизацией Q4_K_M или Q5_K_M (баланс качества и скорости).
4. После скачивания перейди на вкладку **Chat**, выбери модель сверху и
   убедись, что она нормально отвечает на «Привет».
5. Перейди на вкладку **Developer** (значок `</>`).
6. Нажми **Start Server** — внизу должно появиться `Server running on http://localhost:1234`.
7. Запомни **точное имя модели** — оно показано в шапке вкладки Developer.
   Также его можно вытащить запросом:
   ```
   curl http://localhost:1234/v1/models
   ```

> Бот делает многошаговые вызовы LLM (расписание + чат + интенты).
> Чем больше модель — тем точнее результат, но медленнее ответ.
> Для CPU без GPU оптимально брать 7–8B с квантизацией Q4_K_M.

---

## Шаг 3 — Скачать код бота

Открой `cmd`, перейди в нужную папку и помести в неё файлы проекта (или склонируй репозиторий):
```
cd C:\Users\ИМЯ_ПОЛЬЗОВАТЕЛЯ\Documents\Claude\Projects\TManager
```

---

## Шаг 4 — Виртуальное окружение

```
python -m venv venv
venv\Scripts\activate
```

В начале строки должно появиться `(venv)` — окружение активно.
**Все следующие команды выполняй только при активном окружении.**

---

## Шаг 5 — Установка зависимостей

```
pip install -r requirements.txt
```

Это поставит:
- `python-telegram-bot` — клиент Telegram Bot API
- `openai` — мы используем его как HTTP-клиент к LM Studio
- `faster-whisper` — локальный Whisper (вместе с CTranslate2 и PyAV)
- `aiosqlite`, `APScheduler`, `python-dotenv`

> Если pip ругается на `av` (PyAV) — поставь Microsoft Visual C++ Redistributable
> с https://aka.ms/vs/17/release/vc_redist.x64.exe и попробуй ещё раз.

---

## Шаг 6 — Получение токена Telegram

1. Открой Telegram, найди **@BotFather**
2. Напиши `/newbot`
3. Введи имя бота (например `My TManager`)
4. Введи username — должен заканчиваться на `bot` (например `my_tmanager_bot`)
5. BotFather пришлёт токен вида `7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxx`

---

## Шаг 7 — Настройка .env

Скопируй шаблон конфигурации:
```
copy .env.example .env
```

Открой `.env` в Блокноте и заполни:

```env
TELEGRAM_TOKEN=7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxx

LMSTUDIO_BASE_URL=http://localhost:1234/v1
LMSTUDIO_MODEL=qwen/qwen3-8b          # ← сюда точное имя модели из LM Studio

WHISPER_MODEL_SIZE=small               # tiny / base / small / medium / large-v3
WHISPER_DEVICE=cpu                     # cuda — если есть NVIDIA GPU
WHISPER_COMPUTE_TYPE=int8

DATABASE_PATH=tmanager.db
BOT_NAME=TManager
MORNING_MESSAGE_TIME=08:00
EVENING_REVIEW_TIME=21:00
```

> ВАЖНО: не помещай значения в кавычки и не оставляй пробелов вокруг `=`.
> ВАЖНО: никому не отправляй файл `.env` — там твой токен Telegram.

---

## Шаг 8 — Первый запуск

Перед запуском бота **в LM Studio должна быть нажата кнопка Start Server** и
загружена модель. Без этого бот при первом обращении к LLM получит ConnectionError.

```
python bot.py
```

Лог должен выглядеть так:
```
[INFO] database: Database initialized at tmanager.db
[INFO] scheduler_jobs: Scheduler set up: morning=08:00, evening=21:00, reminders every 30 min
[INFO] bot: Scheduler started
[INFO] bot: TManager starting…
```

При **первой** обработке голосового faster-whisper скачает модель Whisper
с Hugging Face (~500 МБ для `small`). Дальше всё будет работать оффлайн.

Открой Telegram, найди своего бота и напиши `/start`.

Останов бота — **Ctrl+C** в терминале.

---

## Шаг 9 — Автозапуск при старте Windows (опционально)

Создай рядом с `bot.py` файл `start_tmanager.bat`:
```bat
@echo off
cd /d C:\Users\ИМЯ_ПОЛЬЗОВАТЕЛЯ\Documents\Claude\Projects\TManager
call venv\Scripts\activate
python bot.py
```

Нажми **Win + R**, введи `shell:startup`, скопируй туда `start_tmanager.bat`.

> Не забудь, что сам LM Studio тоже должен быть запущен — иначе бот не получит
> ответы от LLM. Включить автозапуск LM Studio можно в его настройках:
> *Settings → Developer → Auto-start server on launch*, плюс добавить
> сам LM Studio в автозагрузку Windows.

---

## Решение проблем

**`ConnectionRefusedError` или `httpx.ConnectError` при работе с ИИ**
→ В LM Studio открыта вкладка Developer и нажата кнопка **Start Server**.
→ Модель в LM Studio действительно загружена (показана в шапке Developer).
→ В `.env` правильный `LMSTUDIO_BASE_URL` (по умолчанию `http://localhost:1234/v1`).

**Бот ругается, что модель не найдена**
→ В `.env` имя `LMSTUDIO_MODEL` должно совпадать **символ в символ** с тем,
   что показывает `curl http://localhost:1234/v1/models`.

**LLM отвечает текстом вокруг JSON, и расписание не парсится**
→ Возьми модель помощнее или с лучшим следованием инструкциям (Qwen3, Llama 3.1).
→ Для CPU удобнее размер 7–8B с квантизацией Q4_K_M.

**Голосовые не распознаются / ошибка PyAV**
→ Поставь Microsoft Visual C++ Redistributable
   (https://aka.ms/vs/17/release/vc_redist.x64.exe).
→ При первой расшифровке модель Whisper скачивается несколько минут — подожди.
→ Можно уменьшить `WHISPER_MODEL_SIZE` до `base` или `tiny` (быстрее, менее точно).

**Очень медленные ответы**
→ Это работа на CPU. Помогают: модель меньшего размера (3–4B),
   жёсткая квантизация (Q4_0), GPU-оффлоадинг в LM Studio,
   `WHISPER_DEVICE=cuda` для голоса (если есть NVIDIA GPU).

**`(venv)` не появляется в PowerShell**
→ Используй `cmd` вместо PowerShell, либо в PowerShell:
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

**`ModuleNotFoundError`**
→ Убедись, что виртуальное окружение активно (`(venv)` в начале строки).
→ Запусти `pip install -r requirements.txt` ещё раз.

**Бот не отвечает в Telegram**
→ Проверь, что `python bot.py` запущен и в консоли нет красных ошибок.
→ Токен в `.env` от @BotFather корректный.
→ Напиши `/start` — он создаёт пользователя в БД.
