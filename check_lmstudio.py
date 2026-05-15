"""
Диагностика связи с LM Studio.

Запускать при активном venv

Скрипт сделает три проверки:
  1. Сервер вообще отвечает на http://localhost:1234?
  2. Какие модели LM Studio считает доступными (GET /v1/models)?
  3. Получится ли вызвать chat.completions с моделью из .env?
"""

import sys

import httpx

from config import Config


def main() -> int:
    cfg = Config()
    base = cfg.LMSTUDIO_BASE_URL.rstrip("/")
    model = cfg.LMSTUDIO_MODEL

    print(f"LMSTUDIO_BASE_URL = {base}")
    print(f"LMSTUDIO_MODEL    = {model!r}")
    print()

    # 1. Сервер живой?
    print("[1/3] Проверяю, что сервер LM Studio отвечает...")
    try:
        r = httpx.get(f"{base}/models", timeout=10.0)
    except httpx.ConnectError as e:
        print(f"  ❌ Не могу подключиться: {e}")
        print("     → В LM Studio открой вкладку Developer и нажми Start Server.")
        return 1
    except Exception as e:
        print(f"  ❌ Ошибка сети: {e}")
        return 1
    print(f"  ✓ Сервер ответил, HTTP {r.status_code}")
    print()

    # 2. Какие модели видны?
    print("[2/3] Список загруженных моделей (GET /v1/models)...")
    if r.status_code != 200:
        print(f"  ❌ Сервер вернул не 200, а {r.status_code}.")
        print(f"     Тело ответа: {r.text[:500]}")
        return 1
    try:
        data = r.json().get("data", [])
    except Exception:
        print(f"  ❌ Ответ не JSON: {r.text[:500]}")
        return 1
    if not data:
        print("  ⚠ Список моделей пустой — в LM Studio нет загруженной модели.")
        print("     → Открой вкладку Chat (или Developer), выбери модель в шапке,")
        print("       нажми Load. Затем верни вкладку Developer и ещё раз Start Server.")
        return 1
    print("  ✓ LM Studio видит модели:")
    for m in data:
        print(f"      - {m.get('id')}")
    print()

    # 3. Имя модели из .env совпадает?
    available_ids = {m.get("id") for m in data}
    if model not in available_ids:
        print(f"  ❌ Модель из .env ({model!r}) НЕ совпадает ни с одной из видимых.")
        print(f"     → Положи в .env LMSTUDIO_MODEL ровно как один из id выше.")
        return 1
    print(f"  ✓ Имя из .env совпадает с одной из загруженных моделей.")
    print()

    # 4. Пробный chat.completions
    print("[3/3] Пробую chat.completions...")
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=base,
            api_key=cfg.LMSTUDIO_API_KEY,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Скажи коротко: всё работает."}],
            max_tokens=50,
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        print(f"  ✓ Модель ответила: {text!r}")
        print()
        print("✅ LM Studio в порядке. Можно запускать бота: python bot.py")
        return 0
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", None)
        print(f"  ❌ chat.completions упал: {e}")
        if body:
            print(f"     Тело ответа от сервера: {body[:500]}")
        print()
        print("Возможные причины:")
        print("  - модель видна в /v1/models, но ещё не загружена в RAM")
        print("    (в LM Studio нажми Load на нужной модели)")
        print("  - не хватает оперативной памяти под модель")
        print("  - JIT-loading отключён, а модель не загружена вручную")
        return 1


if __name__ == "__main__":
    sys.exit(main())
