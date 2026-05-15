import asyncio
import base64
import logging
import random
import secrets
import time
from datetime import date, timedelta
from functools import wraps
from typing import Optional

import aiosqlite
import httpx
from flask import Flask, g, jsonify, request
from flask_cors import CORS

from analytics import render_month_chart, render_today_chart, render_week_chart
from ai_client import AIClient
from config import Config
from database import Database

log = logging.getLogger(__name__)

flask_app = Flask(__name__)
CORS(flask_app)

_auth_codes: dict[int, tuple[str, float]] = {}  # telegram_id → (code, expires_at)
_sessions: dict[str, int] = {}                  # token → db_user_id
_code_attempts: dict[str, float] = {}           # ip → last_attempt_time

_config = Config()
_db = Database(_config.DATABASE_PATH)
_ai: Optional[AIClient] = None
_initialized = False

@flask_app.before_request
async def ensure_initialized() -> None:
    global _initialized, _ai
    if _initialized:
        return
    await _db.init()
    try:
        _ai = AIClient(_config)
    except Exception as e:
        log.warning("AI init skipped: %s", e)
    _initialized = True

def require_auth(f):
    @wraps(f)
    async def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        token = auth[7:]
        user_id = _sessions.get(token)
        if user_id is None:
            return jsonify({"error": "Invalid or expired token"}), 401
        g.user_id = user_id
        return await f(*args, **kwargs)
    return wrapper

async def _find_user_by_input(raw: str) -> Optional[dict]:
    username = raw.lstrip("@").strip()
    async with aiosqlite.connect(_db.path) as conn:
        conn.row_factory = aiosqlite.Row
        if username.isdigit():
            cur = await conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (int(username),)
            )
            row = await cur.fetchone()
            if row:
                return dict(row)
        cur = await conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

async def _get_full_user(user_id: int) -> dict:
    async with aiosqlite.connect(_db.path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return {}
        return dict(row)

@flask_app.route("/auth/request_code", methods=["POST"])
async def request_code():
    ip = request.remote_addr
    if time.time() - _code_attempts.get(ip, 0.0) < 60:
        return jsonify({"error": "Too many requests. Wait 1 minute."}), 429
    _code_attempts[ip] = time.time()

    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    if not username:
        return jsonify({"error": "username required"}), 400

    user = await _find_user_by_input(username)
    if not user:
        return jsonify({"error": "User not found. Start the bot with /start first."}), 404

    code = str(random.randint(100000, 999999))
    _auth_codes[user["telegram_id"]] = (code, time.time() + 300)

    tg_url = f"https://api.telegram.org/bot{_config.TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(tg_url, json={
            "chat_id": user["telegram_id"],
            "text": f"🔐 Код входа в приложение: <b>{code}</b>\n(действует 5 минут)",
            "parse_mode": "HTML",
        })
        if resp.status_code != 200:
            log.warning("Telegram sendMessage failed: %s", resp.text)

    return jsonify({"ok": True})

@flask_app.route("/auth/verify_code", methods=["POST"])
async def verify_code():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    code_input = data.get("code", "")

    user = await _find_user_by_input(username)
    if not user:
        return jsonify({"error": "User not found"}), 404

    stored = _auth_codes.get(user["telegram_id"])
    if not stored:
        return jsonify({"error": "No code was requested. Call /auth/request_code first."}), 400

    code, expires_at = stored
    if time.time() > expires_at:
        _auth_codes.pop(user["telegram_id"], None)
        return jsonify({"error": "Code expired. Request a new one."}), 400

    if code_input != code:
        return jsonify({"error": "Invalid code"}), 400

    _auth_codes.pop(user["telegram_id"], None)
    token = secrets.token_urlsafe(32)
    _sessions[token] = user["id"]
    return jsonify({"token": token, "user_id": user["id"]})

@flask_app.route("/tasks", methods=["GET"])
@require_auth
async def get_tasks():
    tasks = await _db.get_tasks(g.user_id)
    return jsonify({"tasks": tasks})

@flask_app.route("/tasks", methods=["POST"])
@require_auth
async def add_task():
    data = request.get_json(silent=True) or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400

    task_id = await _db.add_task(
        user_id=g.user_id,
        title=title,
        due_date=data.get("due_date"),
        time=data.get("time"),
        priority=data.get("priority"),
        source="desktop",
    )
    tasks = await _db.get_tasks(g.user_id)
    task = next((t for t in tasks if t["id"] == task_id), {"id": task_id, "title": title})
    return jsonify(task), 201

@flask_app.route("/tasks/<int:task_id>/done", methods=["POST"])
@require_auth
async def complete_task(task_id):
    task = await _db.complete_task(task_id, g.user_id)
    if not task:
        return jsonify({"error": "Task not found or already completed"}), 404
    return jsonify(task)

@flask_app.route("/tasks/<int:task_id>", methods=["DELETE"])
@require_auth
async def delete_task(task_id):
    task = await _db.delete_task(task_id, g.user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)

@flask_app.route("/schedule/week", methods=["GET"])
@require_auth
async def get_week_schedule():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    grouped = await _db.get_week_tasks_grouped(g.user_id, monday)
    return jsonify({"week_start": monday.isoformat(), "days": grouped})

@flask_app.route("/notes", methods=["GET"])
@require_auth
async def get_notes():
    notes = await _db.get_notes(g.user_id)
    return jsonify({"notes": notes})

@flask_app.route("/notes", methods=["POST"])
@require_auth
async def add_note():
    data = request.get_json(silent=True) or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "content required"}), 400
    note_id = await _db.add_note(g.user_id, content)
    return jsonify({"id": note_id, "content": content}), 201

@flask_app.route("/notes/<int:note_id>", methods=["DELETE"])
@require_auth
async def delete_note(note_id):
    ok = await _db.delete_note(note_id, g.user_id)
    if not ok:
        return jsonify({"error": "Note not found"}), 404
    return jsonify({"ok": True})

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()

@flask_app.route("/analytics/today", methods=["GET"])
@require_auth
async def analytics_today():
    today = date.today()
    stat = await _db.daily_stats(g.user_id, today)
    png = await asyncio.to_thread(render_today_chart, stat)
    return jsonify({"png_base64": _b64(png), "stat": stat})

@flask_app.route("/analytics/week", methods=["GET"])
@require_auth
async def analytics_week():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    stats = await _db.daily_stats_range(g.user_id, monday, monday + timedelta(days=6))
    png = await asyncio.to_thread(render_week_chart, stats, today)
    return jsonify({"png_base64": _b64(png), "stats": stats})

@flask_app.route("/analytics/month", methods=["GET"])
@require_auth
async def analytics_month():
    today = date.today()
    month_start = today.replace(day=1)
    if today.month == 12:
        month_end = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        month_end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    stats = await _db.daily_stats_range(g.user_id, month_start, month_end)
    png = await asyncio.to_thread(render_month_chart, stats, today)
    return jsonify({"png_base64": _b64(png), "stats": stats})

@flask_app.route("/ai/chat", methods=["POST"])
@require_auth
async def ai_chat():
    if _ai is None:
        return jsonify({"error": "AI service unavailable"}), 503

    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    history = data.get("history", [])

    full_user = await _get_full_user(g.user_id)
    if not full_user:
        return jsonify({"error": "User not found"}), 404

    telegram_id = full_user["telegram_id"]
    personality = full_user.get("personality") or "soft"

    context = await _db.get_user_summary_context(telegram_id)

    result = await asyncio.to_thread(
        _ai.process_user_intent, message, context, history, personality
    )

    intent = result.get("intent", "chat")
    actions_done: list[str] = []

    if intent == "add_tasks":
        for task_data in result.get("tasks", []):
            title = (task_data.get("title") or "").strip()
            if not title:
                continue
            await _db.add_task(
                user_id=g.user_id,
                title=title,
                due_date=task_data.get("due_date"),
                time=task_data.get("time"),
                recurring=task_data.get("recurring"),
                priority=task_data.get("priority"),
                source="ai_desktop",
            )
            actions_done.append(f"добавлено: {title}")

    elif intent == "done_tasks":
        for tid in result.get("done_task_ids", []):
            try:
                task = await _db.complete_task(int(tid), g.user_id)
                if task:
                    actions_done.append(f"выполнено: {task['title']}")
            except Exception:
                pass

    elif intent == "delete_tasks":
        for tid in result.get("delete_task_ids", []):
            try:
                task = await _db.delete_task(int(tid), g.user_id)
                if task:
                    actions_done.append(f"удалено: {task['title']}")
            except Exception:
                pass

    elif intent == "add_note":
        note_text = result.get("note_text") or ""
        if note_text:
            await _db.add_note(g.user_id, note_text)
            actions_done.append(f"заметка: {note_text[:50]}")

    elif intent == "delete_note":
        for nid in result.get("delete_note_ids", []):
            try:
                await _db.delete_note(int(nid), g.user_id)
                actions_done.append(f"заметка {nid} удалена")
            except Exception:
                pass

    elif intent == "set_priority":
        for change in result.get("priority_changes", []):
            try:
                task = await _db.set_task_priority(
                    int(change["task_id"]), g.user_id, change.get("priority")
                )
                if task:
                    actions_done.append(f"приоритет: {task['title']}")
            except Exception:
                pass

    result["actions_done"] = actions_done
    return jsonify(result)
