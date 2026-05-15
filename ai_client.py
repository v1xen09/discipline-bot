import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from openai import OpenAI

WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]


def _part_of_day(hour: int) -> str:
    if hour < 6:
        return "ночь"
    if hour < 12:
        return "утро"
    if hour < 18:
        return "день"
    return "вечер"


def _now_context() -> str:
    """Текущий день/дата/время — без этого модель не знает «сегодня»."""
    now = datetime.now()
    return (
        f"Сейчас {WEEKDAYS_RU[now.weekday()]}, "
        f"{now.strftime('%d.%m.%Y %H:%M')} ({_part_of_day(now.hour)})."
    )

from config import Config

log = logging.getLogger(__name__)


SYSTEM_BASE = """/no_think
Ты — TManager, ИИ-помощник по продуктивности.
Ты помнишь историю пользователя (она передаётся в контексте) и отслеживаешь
его задачи и серии.

Общие правила работы:
1. Ты НИКОГДА не придумываешь задачи или события, которых нет в контексте.
2. Используй эмодзи умеренно — только там, где они усиливают смысл.
3. Ответы лаконичны (2–4 предложения), если пользователь не просит подробностей.
4. Никаких рассуждений в <think>…</think> — отвечай сразу по существу.
"""

PERSONALITIES: dict[str, str] = {
    "soft": (
        "Стиль общения — мягкий и тёплый. Ты дружелюбен, чуток, поддерживаешь, "
        "но не сюсюкаешь и не льстишь пустыми словами. Радуешься успехам, "
        "сочувствуешь срывам, никогда не давишь."
    ),
    "neutral": (
        "Стиль общения — нейтральный и деловой. Без эмоциональных ярлыков, "
        "без «молодец/жалко». Сухой, ясный, по делу. Минимум эмодзи."
    ),
    "strict": (
        "Стиль общения — жёсткий и требовательный. Ты прямой и честный, "
        "не оправдываешь прокрастинацию, не жалеешь, ставишь дисциплину "
        "выше комфорта. Не оскорбляешь и не унижаешь, но и не утешаешь, "
        "если человек не делает то, что обещал. Когда видишь успех — "
        "признаёшь его коротко, без восторгов."
    ),
    "playful": (
        "Стиль общения — игривый и лёгкий. Ты шутишь, иронизируешь над "
        "ситуациями, используешь сравнения и метафоры. Но не превращаешь "
        "разговор в стендап — суть не теряется. Эмодзи приветствуются."
    ),
}

PERSONALITY_LABELS: dict[str, str] = {
    "soft": "🫂 Мягкий",
    "neutral": "📐 Нейтральный",
    "strict": "🪖 Требовательный",
    "playful": "🎭 Весёлый",
}


def build_system_prompt(personality: str = "soft") -> str:
    """Системный промпт под конкретный характер."""
    persona = PERSONALITIES.get(personality, PERSONALITIES["soft"])
    return f"{SYSTEM_BASE}\n{persona}"


# Совместимость для остальных модулей, которые импортировали SYSTEM_PROMPT.
SYSTEM_PROMPT = build_system_prompt("soft")


def _extract_balanced_json(text: str) -> Optional[str]:
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                return text[start:i + 1]
    return None


def _strip_json(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if "```" in text:
            text = text.rsplit("```", 1)[0]
        text = text.strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    extracted = _extract_balanced_json(text)
    if extracted:
        text = extracted
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def _close_truncated_json(text: str) -> str:
    """Закрывает незавершённые скобки в JSON, обрезанном на max_tokens."""
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    suffix = '"' if in_string else ""
    suffix += "".join(reversed(stack))
    return text + suffix


def _try_parse_json(raw: str) -> Optional[dict]:
    cleaned = _strip_json(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning("JSON parse failed at pos %s: %s | snippet: %r",
                    e.pos, e.msg, cleaned[max(0, e.pos - 40):e.pos + 40])
        repaired = _close_truncated_json(cleaned)
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
        try:
            result = json.loads(repaired)
            log.info("JSON repaired (truncated response recovered)")
            return result
        except json.JSONDecodeError:
            return None


class AIClient:
    def __init__(self, config: Config) -> None:
        # trust_env=False важно: иначе httpx подхватит HTTP_PROXY/HTTPS_PROXY
        # из системы и попытается ходить к localhost через прокси (или VPN),
        # запрос до LM Studio не дойдёт и мы получим 503/таймаут от прокси.
        http_client = httpx.Client(
            trust_env=False,
            timeout=httpx.Timeout(config.LMSTUDIO_TIMEOUT, connect=10.0),
        )
        self.client = OpenAI(
            base_url=config.LMSTUDIO_BASE_URL,
            api_key=config.LMSTUDIO_API_KEY,
            http_client=http_client,
        )
        self.model = config.LMSTUDIO_MODEL

    def _call(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        max_tokens: int = 1500,
        temperature: float = 0.7,
    ) -> str:
        return self._call_lmstudio(messages, system, max_tokens, temperature)

    def _call_lmstudio(
        self,
        messages: list[dict],
        system: Optional[str],
        max_tokens: int,
        temperature: float,
    ) -> str:
        # Подмешиваем текущее время — модель сама не знает дату/день недели.
        time_block = _now_context()
        system_full = f"{system}\n\n{time_block}" if system else time_block

        full_messages: list[dict] = [{"role": "system", "content": system_full}]
        full_messages.extend(messages)

        # Qwen3: /no_think работает только в конце последнего user-сообщения.
        if full_messages and full_messages[-1].get("role") == "user":
            last = dict(full_messages[-1])
            last["content"] = f"{last['content']}\n\n/no_think"
            full_messages = full_messages[:-1] + [last]

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=full_messages,
                max_tokens=max_tokens,
                temperature=temperature,
                # LM Studio: отключает reasoning на уровне template; игнорируется если модель не поддерживает.
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
        except Exception as e:
            body = getattr(getattr(e, "response", None), "text", None)
            if body:
                log.error("LM Studio call failed: %s | body: %s", e, body[:500])
            else:
                log.error("LM Studio call failed: %s", e)
            raise

        raw = response.choices[0].message.content or ""
        finish = response.choices[0].finish_reason
        log.debug("LM Studio raw len=%d finish=%s", len(raw), finish)

        content = raw

        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)

        if "<think>" in content:
            before, _, after = content.partition("<think>")
            if before.strip():
                content = before
            else:
                content = after

        content = content.strip()

        if not content:
            log.warning(
                "LM Studio returned no usable content (finish=%s raw_len=%d) head=%r",
                finish, len(raw), raw[:200],
            )
            content = (
                "Хм, не сформулировал ответ — модель ушла в размышления и не успела "
                "выдать результат. Попробуй ещё раз или перефразируй."
            )

        return content

    def generate_schedule(
        self, user_request: str, context: str = "", target_monday: "date | None" = None
    ) -> dict:
        today = date.today()
        monday = target_monday or (today - timedelta(days=today.weekday()))
        sunday = monday + timedelta(days=6)
        today_ru = WEEKDAYS_RU[today.weekday()]
        is_future_week = monday > today

        context_block = f"\n\nКонтекст пользователя:\n{context}" if context else ""

        past_note = (
            "Прошедшие дни этой недели не планируй — оставь их пустыми массивами.\n"
            if not is_future_week else ""
        )

        prompt = f"""Пользователь просит составить расписание на неделю.
Запрос: «{user_request}»{context_block}

Сегодня — {today_ru}, {today.strftime('%d.%m.%Y')}.
Планируемая неделя: с {monday.strftime('%d.%m.%Y')} (пн) по {sunday.strftime('%d.%m.%Y')} (вс).
{past_note}
КРИТИЧНО — НЕ выдумывай задачи:
- В план попадает ТОЛЬКО то, что пользователь явно описал в запросе.
- Если запрос короткий — расписание тоже короткое. Это нормально.
  НЕ заполняй пустые дни «на всякий случай», НЕ добавляй приёмы пищи,
  прогулки, отдых, сон, медитации, чтение и прочие активности, если
  пользователь о них не сказал.
- Если пользователь упомянул только одну активность — расписание
  состоит ровно из неё (и только в дни, где она уместна).

ПРО НАЗВАНИЯ ЗАДАЧ:
- Маркеры вида [id=N] в контексте — служебные. НЕ включай их в поле "task".
- КОРОТКИЕ (1–3 слова), повторяющиеся для одной и той же привычки.
- Одна привычка = ОДНО название. «Алгебра» во все дни недели; НЕ «Решить
  5 задач», «Прочитать главу» — это разные задачи, ломают учёт.
- Детали активности — в "description", а не в "task".
- Поле "time" опционально: если пользователь не указал время, оставь
  пустую строку "" или null.

Каждый пункт плана должен иметь поле "type":
- "task" — активность, требующая усилия (учёба, спорт, работа, проект, тренировка).
- "reminder" — фоновый или пассивный пункт (завтрак, обед, ужин, прогулка, перерыв, отдых, сон, медитация, разминка, зарядка без нагрузки).

Верни расписание СТРОГО в виде JSON (без markdown, без пояснений):
{{
  "target_week_start": "YYYY-MM-DD или null",
  "monday": [
    {{"time": "08:00", "task": "Завтрак", "description": "", "type": "reminder"}},
    {{"time": "09:00", "task": "Алгебра", "description": "задачи из главы 5", "type": "task"}}
  ],
  "tuesday":   [...],
  "wednesday": [...],
  "thursday":  [...],
  "friday":    [...],
  "saturday":  [...],
  "sunday":    [...]
}}

target_week_start: если пользователь указал конкретную дату начала недели или
конкретный месяц/число — верни понедельник той недели в формате YYYY-MM-DD.
Иначе null (бот сам знает, какая неделя выбрана).

Правила:
- Каждый день 3–6 пунктов, реалистично распределённых по времени.
- Учитывай контекст пользователя, его текущие задачи и серии.
- Выходные можно сделать легче.
- ВАЖНО: ответ должен быть только валидным JSON, без пояснений и markdown.
"""
        raw = self._call(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.6,
        )
        parsed = _try_parse_json(raw)
        if parsed is None:
            log.warning("Failed to parse schedule JSON, returning raw: %s", raw[:300])
            return {"raw": raw}
        return parsed

    def generate_motivation(
        self, context: str, trigger: str = "morning", personality: str = "soft",
    ) -> str:
        trigger_hints = {
            "morning": "Это утреннее приветствие. Зарядь на день, напомни о предстоящих задачах.",
            "evening": "Это вечерний итог дня. Подведи итоги, отметь достижения, подготовь к завтрашнему.",
            "overdue": "У пользователя есть просроченные задачи. Мягко, но прямо скажи об этом и предложи начать прямо сейчас.",
            "reminder": (
                "Это дневное напоминание о невыполненных задачах. Упомяни конкретные дела из контекста. "
                "Не паникуй и не давай длинных инструкций — просто напомни, что дела ждут, и дай лёгкий импульс действовать."
            ),
            "rate_high":
                "Коэффициент дня высокий (≥80%). Признай результат — коротко, без преувеличений.",
            "rate_mid":
                "Коэффициент дня средний (50–80%). Отметь, что есть прогресс, и предложи добить остаток.",
            "rate_low":
                "Коэффициент дня низкий (<50%). Согласно своему характеру: либо мягко поддержи и предложи маленький шаг, "
                "либо честно укажи на провал — но НЕ оскорбляй и НЕ обесценивай. Спроси, что мешает.",
        }
        hint = trigger_hints.get(trigger, "Напиши поддерживающее сообщение.")

        prompt = f"""Контекст пользователя:\n{context}\n\n{hint}\n
Ответ — 2–3 предложения, тёплые, честные. Используй данные из контекста."""

        return self._call(
            messages=[{"role": "user", "content": prompt}],
            system=build_system_prompt(personality),
            max_tokens=300,
            temperature=0.8,
        )

    def chat(
        self,
        user_message: str,
        context: str,
        history: Optional[list[dict]] = None,
        personality: str = "soft",
    ) -> str:
        system = build_system_prompt(personality)
        if context:
            system += f"\n\n=== Контекст пользователя ===\n{context}"

        messages: list[dict] = []
        if history:
            messages.extend(history[-6:])  # keep last 3 exchanges
        messages.append({"role": "user", "content": user_message})

        return self._call(messages=messages, system=system, max_tokens=1200, temperature=0.7)

    def process_user_intent(
        self,
        message: str,
        context: str,
        history: Optional[list[dict]] = None,
        personality: str = "soft",
    ) -> dict:
        """Маршрутизатор для голоса и текста: за один вызов LLM определяет intent и формирует reply."""
        history_block = ""
        if history:
            recent = history[-6:]
            lines = []
            for h in recent:
                speaker = "Пользователь" if h.get("role") == "user" else "Ты"
                lines.append(f"{speaker}: {h.get('content', '')}")
            history_block = "\n\nНедавняя переписка:\n" + "\n".join(lines)

        _today = date.today()
        today_iso = _today.isoformat()
        tomorrow_iso = (_today + timedelta(days=1)).isoformat()
        day_after_iso = (_today + timedelta(days=2)).isoformat()

        prompt = f"""Сообщение пользователя:
«{message}»{history_block}

Контекст пользователя:
{context}

Определи намерение и СРАЗУ сформулируй ответ пользователю.
Верни JSON (без markdown, без пояснений):
{{
  "intent": "add_tasks" | "done_tasks" | "delete_tasks" | "schedule" | "modify_schedule" | "add_note" | "delete_note" | "set_priority" | "set_reminder" | "set_task_time" | "motivation" | "chat",
  "tasks": [{{"title": "...", "due_date": "YYYY-MM-DD или null", "time": "HH:MM или null", "recurring": "daily|weekly|null", "priority": "high|medium|low|null", "notify_before": null}}],
  "done_task_ids": [12, 15],
  "done_task_titles": ["Зарядка"],
  "delete_task_ids": [7],
  "delete_task_titles": ["Зал"],
  "note_text": "текст заметки или null",
  "delete_note_ids": [3, 7],
  "schedule_request": "текст запроса на расписание или null",
  "schedule_week_offset": 0,
  "schedule_changes": [],
  "priority_changes": [{{"task_id": 5, "priority": "high|medium|low|null"}}],
  "reminder_changes": [{{"task_id": 5, "minutes": 30}}],
  "time_changes": [{{"task_id": 5, "time": "HH:MM или null"}}],
  "reply": "ответ пользователю в твоём стиле, 2–4 предложения"
}}

Правила распознавания intent (ВНИМАТЕЛЬНО, это часто путают):

- add_tasks: пользователь называет ОДНУ или НЕСКОЛЬКО конкретных задач,
  которые надо добавить. Если он указал день/время («завтра», «в пятницу
  в 14:00», «сегодня вечером») — это всё равно add_tasks, просто заполняй
  поля due_date и time. НЕ пытайся «составить план дня» из одной задачи.
  due_date — СТРОГО формат YYYY-MM-DD или null. НИКОГДА не пиши слова
  «сегодня», «завтра» и т.п. — только конкретную дату, опираясь на дату
  из контекста. Сегодня={today_iso}, завтра={tomorrow_iso}, послезавтра={day_after_iso}.
  «в [день недели]» — ближайшее будущее вхождение этого дня.
  notify_before — число минут до начала задачи, если пользователь просит напомнить
  (30, 60, 120 и т.п.). Требует, чтобы у задачи было time. Иначе null.
  Примеры:
    • «надо купить хлеб» → add_tasks, due_date=null, time=null, notify_before=null
    • «запланируй сегодня купить хлеб» → add_tasks, due_date={today_iso}, time=null, notify_before=null
    • «добавь на завтра в 15:00 встречу с врачом» → add_tasks, due_date={tomorrow_iso}, time=15:00, notify_before=null
    • «добавь встречу в 15:00 и напомни за 30 минут» → add_tasks, time=15:00, notify_before=30
    • «надо сделать зарядку и купить хлеб» → add_tasks с двумя элементами

- schedule: пользователь ЯВНО просит составить план/расписание на несколько
  дней или на всю неделю С НУЛЯ. Только когда есть слова вроде «расписание»,
  «план на неделю», «распиши неделю», «составь график». Перетирает текущий план.
  Примеры:
    • «составь расписание на неделю» → schedule
    • «распиши мне план на эти 3 дня по учёбе» → schedule
  ВАЖНО: «запланируй на сегодня X» — это НЕ schedule, это add_tasks.

- modify_schedule: правки В ТЕКУЩЕМ плане, без пересборки. Перенос или
  удаление существующих пунктов между днями. Если пользователь добавляет
  новую задачу на день — это add_tasks (а не modify_schedule add).
    • «убери алгебру в среду» → modify_schedule remove
    • «перенеси встречу с врачом с пятницы на четверг» → modify_schedule move

- done_tasks: сообщает о выполнении уже существующей задачи.
- delete_tasks: просит удалить существующую задачу из списка.
- add_note: просит запомнить мысль, идею или факт НЕ как задачу, а как заметку.
  Ключевые слова: «запомни», «сохрани», «отметь», «запиши в заметки», «сделай заметку».
  note_text — сформулированный текст заметки (одно-два предложения, суть).
  Примеры:
    • «запомни, что я хочу прочитать книгу "Атомные привычки"» → add_note
    • «сохрани идею: переосмыслить архитектуру проекта» → add_note
    • «запиши — нужно позвонить врачу на следующей неделе» → add_note
    НЕ add_note: «добавь задачу позвонить врачу» — это add_tasks.
- delete_note: просит удалить заметку. delete_note_ids берёт из «Последние заметки (с ID)» в контексте.
- set_priority: пользователь явно просит изменить приоритет СУЩЕСТВУЮЩЕЙ задачи.
  priority_changes — список изменений; task_id берёт из «Активные задачи (с ID)».
  priority=null означает «убрать приоритет».
  Примеры:
    • «поставь высокий приоритет задаче купить хлеб» → set_priority, priority=high
    • «убери приоритет с отчёта» → set_priority, priority=null
    • «сделай задачу зарядка низкоприоритетной» → set_priority, priority=low
- set_reminder: установить или убрать напоминание для СУЩЕСТВУЮЩЕЙ задачи.
  task_id берётся из «Активные задачи (с ID)».
  minutes: число минут до начала задачи (10/15/30/60/90/120 или иное разумное).
  0 = убрать напоминание.
  Если у задачи нет ⏰ времени (нет суффикса ⏰HH:MM в контексте) — в reply
  честно скажи, что нужно сначала задать время задаче.
  Примеры:
    • «напомни за 30 минут до встречи с врачом» → set_reminder, minutes=30
    • «поставь будильник за час до тренировки» → set_reminder, minutes=60
    • «убери напоминание с задачи купить хлеб» → set_reminder, minutes=0

- set_task_time: задать или изменить время для СУЩЕСТВУЮЩЕЙ задачи.
  task_id из «Активные задачи (с ID)». time: "HH:MM" или null (убрать время).
  Примеры:
    • «встреча с врачом теперь в 15:00» → set_task_time, time="15:00"
    • «убери время с задачи купить хлеб» → set_task_time, time=null

- motivation: ищет поддержку, признание.
- chat: вопросы, разговоры, благодарности, всё остальное.

ПРАВИЛА для поля priority в add_tasks:
- Заполняй ТОЛЬКО если пользователь ЯВНО упомянул приоритет: «срочно», «важно»,
  «высокий приоритет», «не срочно», «низкий приоритет» и т.п.
- НЕ присваивай приоритет автоматически на основе логики.
- Если пользователь не упоминал приоритет — оставляй null.

КРИТИЧНО для done_tasks / delete_tasks:
- ВСЕГДА заполняй done_task_titles / delete_task_titles точным названием задачи
  как написал пользователь («удали Зал» → delete_task_titles: ["Зал"]).
  Бот найдёт задачу по этому тексту — это надёжнее числовых ID.
- done_task_ids / delete_task_ids оставляй пустыми — они не используются когда
  есть titles.
- Сопоставление по СМЫСЛУ: «удали покупки» → delete_task_titles: ["Купить хлеб"].
- В поле reply НЕ пиши «удалил» / «отметил» — бот сформирует подтверждение
  сам после реальной операции. Пиши только вводную реакцию.

schedule_week_offset: 0 = эта неделя (по умолчанию), 1 = следующая.
Ставь 1 если пользователь написал «следующая неделя», «на той неделе» и т.п.

КРИТИЧНО для modify_schedule:
- date, from_date, to_date — КОНКРЕТНАЯ ДАТА в формате YYYY-MM-DD.
  Используй реальную календарную дату, НИКОГДА не название дня недели.
  Сегодняшняя дата и день недели указаны в начале контекста — считай от них.
  Пример: если сегодня воскресенье 10.05, «завтра» = 2026-05-11, «следующий пн» = 2026-05-11.
- op = "add"     требует поля: date, task; time и description опциональны.
- op = "remove"  требует поля: date, task (название как оно в плане).
- op = "move"    требует поля: from_date, to_date, task; new_time опционально.
- Можно вернуть НЕСКОЛЬКО изменений за один запрос, если пользователь
  попросил несколько правок сразу.

КРИТИЧНО — разграничение schedule и modify_schedule:
- intent="schedule"        → schedule_changes ВСЕГДА = [] (пустой список).
- intent="modify_schedule" → schedule_request ВСЕГДА = null.
НИКОГДА не заполняй оба поля одновременно — это несовместимые intent'ы.

ВАЖНО:
- Поля заполняются ТОЛЬКО если intent им соответствует. Иначе — пустые / null.
- Ответ — только валидный JSON, без markdown и без пояснений.
"""
        raw = self._call(
            messages=[{"role": "user", "content": prompt}],
            system=build_system_prompt(personality),
            max_tokens=5000,
            temperature=0.4,
        )
        parsed = _try_parse_json(raw)
        if parsed is None:
            log.warning("Failed to parse intent JSON: %s", raw[:300])
            return {
                "intent": "chat",
                "tasks": [],
                "done_task_titles": [],
                "delete_task_titles": [],
                "schedule_request": None,
                "reply": "",
            }
        # Модель может пропустить пустые поля — проставляем дефолты.
        parsed.setdefault("tasks", [])
        parsed.setdefault("done_task_ids", [])
        parsed.setdefault("delete_task_ids", [])
        parsed.setdefault("done_task_titles", [])
        parsed.setdefault("delete_task_titles", [])
        parsed.setdefault("note_text", None)
        parsed.setdefault("delete_note_ids", [])
        parsed.setdefault("schedule_request", None)
        parsed.setdefault("schedule_week_offset", 0)
        parsed.setdefault("schedule_changes", [])
        parsed.setdefault("priority_changes", [])
        parsed.setdefault("reminder_changes", [])
        parsed.setdefault("time_changes", [])
        parsed.setdefault("reply", "")
        parsed.setdefault("intent", "chat")
        return parsed

    # Обратная совместимость с voice_message_handler.
    def process_voice_transcript(self, transcript: str, context: str) -> dict:
        return self.process_user_intent(transcript, context)

    def synthesize_diary_entry(self, recent_events: str, user_context: str) -> Optional[str]:
        prompt = f"""На основе недавних событий пользователя сформулируй краткую запись в дневник (1–2 предложения).
Это внутренняя заметка для тебя о характере, привычках и прогрессе пользователя.

Недавние события:
{recent_events}

Контекст:
{user_context}

Дай только текст записи, без пояснений. Пиши от первого лица ("я заметил...")."""
        try:
            return self._call(
                messages=[{"role": "user", "content": prompt}],
                system=SYSTEM_PROMPT,
                max_tokens=200,
                temperature=0.7,
            )
        except Exception as e:
            log.warning("Diary synthesis failed: %s", e)
            return None
