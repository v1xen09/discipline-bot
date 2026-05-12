"""
Database layer for TManager.
Uses SQLite via aiosqlite for async access.

Tables:
    users       — registered Telegram users + their timezone
    tasks       — individual tasks/habits with deadlines
    schedules   — AI-generated weekly schedules (stored as JSON)
    streaks     — per-task streak counters
    diary       — AI observations about the user (memory system)
    completions — log of every task completion (for streak math)
"""

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import aiosqlite

log = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str = "tmanager.db") -> None:
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS users (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username    TEXT,
                    full_name   TEXT,
                    timezone    TEXT NOT NULL DEFAULT 'UTC',
                    personality TEXT NOT NULL DEFAULT 'soft',
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title         TEXT NOT NULL,
                    description   TEXT,
                    due_date      TEXT,          -- ISO date YYYY-MM-DD, nullable = без срока
                    time          TEXT,          -- HH:MM, опционально, при наличии due_date
                    recurring     TEXT,          -- NULL | daily | weekly | workdays
                    completed     INTEGER NOT NULL DEFAULT 0,
                    completed_at  TEXT,
                    source        TEXT DEFAULT 'manual',  -- manual | schedule | voice | text
                    from_schedule INTEGER NOT NULL DEFAULT 0,  -- 1 = пришло из недельного плана; 0 = доп. задача
                    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    week_start      TEXT NOT NULL, -- YYYY-MM-DD (Monday)
                    schedule_json   TEXT NOT NULL,
                    previous_json   TEXT,          -- предыдущее состояние для отката (1 уровень)
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(user_id, week_start)
                );

                CREATE TABLE IF NOT EXISTS streaks (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    current_streak  INTEGER NOT NULL DEFAULT 0,
                    longest_streak  INTEGER NOT NULL DEFAULT 0,
                    last_completed  TEXT,   -- ISO date
                    UNIQUE(user_id, task_id)
                );

                CREATE TABLE IF NOT EXISTS completions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    completed_on TEXT NOT NULL,  -- ISO date
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS diary (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    content     TEXT NOT NULL,
                    entry_type  TEXT NOT NULL DEFAULT 'observation',  -- observation | motivation | reflection | summary
                    importance  INTEGER NOT NULL DEFAULT 5,           -- 1-10
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS notes (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    content    TEXT NOT NULL,
                    source     TEXT NOT NULL DEFAULT 'manual',  -- manual | ai
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_user_due   ON tasks(user_id, due_date);
                CREATE INDEX IF NOT EXISTS idx_diary_user       ON diary(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_completions_user ON completions(user_id, completed_on);
                CREATE INDEX IF NOT EXISTS idx_notes_user       ON notes(user_id, created_at DESC);
            """)
            # Лёгкие миграции для уже существующих БД — CREATE IF NOT EXISTS не
            # добавляет новые столбцы. Пробуем добавить, ловим OperationalError
            # если столбец уже есть.
            for stmt in (
                "ALTER TABLE users ADD COLUMN personality TEXT NOT NULL DEFAULT 'soft'",
                "ALTER TABLE schedules ADD COLUMN previous_json TEXT",
                "ALTER TABLE tasks ADD COLUMN time TEXT",
                "ALTER TABLE tasks ADD COLUMN from_schedule INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE tasks ADD COLUMN priority TEXT DEFAULT NULL",
                "ALTER TABLE tasks ADD COLUMN notify_before INTEGER DEFAULT NULL",
                "ALTER TABLE tasks ADD COLUMN remind_at TEXT DEFAULT NULL",
                "ALTER TABLE tasks ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN notify_morning TEXT DEFAULT '08:00'",
                "ALTER TABLE users ADD COLUMN notify_evening TEXT DEFAULT '21:00'",
                "ALTER TABLE users ADD COLUMN notify_reminders TEXT DEFAULT '12:00,17:00'",
                "ALTER TABLE users ADD COLUMN city TEXT DEFAULT NULL",
                "ALTER TABLE users ADD COLUMN lat REAL DEFAULT NULL",
                "ALTER TABLE users ADD COLUMN lon REAL DEFAULT NULL",
                "ALTER TABLE users ADD COLUMN notify_weather INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    await db.execute(stmt)
                except Exception:
                    pass  # столбец уже есть
            await db.commit()
        log.info("Database initialized at %s", self.path)

    async def get_or_create_user(
        self,
        telegram_id: int,
        username: Optional[str],
        full_name: Optional[str],
    ) -> dict:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
            await db.execute(
                "INSERT INTO users (telegram_id, username, full_name) VALUES (?, ?, ?)",
                (telegram_id, username, full_name),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            )
            row = await cursor.fetchone()
            return dict(row)

    async def get_user_by_telegram_id(self, telegram_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_all_user_ids(self) -> list[int]:
        """Return all telegram_id values (for scheduler broadcasts)."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT telegram_id FROM users")
            return [r[0] for r in await cur.fetchall()]

    async def set_personality(self, telegram_id: int, personality: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET personality = ? WHERE telegram_id = ?",
                (personality, telegram_id),
            )
            await db.commit()

    async def get_personality(self, telegram_id: int) -> str:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT personality FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cur.fetchone()
            return (row[0] if row else "soft") or "soft"

    async def clear_user_history(self, telegram_id: int) -> dict:
        """Стирает данные пользователя (задачи/стрики/расписание/дневник), но не аккаунт и настройки."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            )
            row = await cur.fetchone()
            if not row:
                return {}
            user_id = row[0]

            counts: dict[str, int] = {}
            for table in ("tasks", "completions", "streaks", "schedules", "diary"):
                cur = await db.execute(
                    f"DELETE FROM {table} WHERE user_id = ?", (user_id,)
                )
                counts[table] = cur.rowcount or 0
            await db.commit()
            return counts

    async def add_task(
        self,
        user_id: int,
        title: str,
        description: str = "",
        due_date: Optional[str] = None,
        time: Optional[str] = None,
        recurring: Optional[str] = None,
        source: str = "manual",
        from_schedule: bool = False,
        priority: Optional[str] = None,
    ) -> int:
        valid_priorities = {"high", "medium", "low", None}
        if priority not in valid_priorities:
            priority = None
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """INSERT INTO tasks
                       (user_id, title, description, due_date, time, recurring, source, from_schedule, priority)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, title, description, due_date, time, recurring, source,
                    1 if from_schedule else 0, priority,
                ),
            )
            await db.commit()
            return cur.lastrowid  # type: ignore

    async def get_week_tasks_grouped(
        self, user_id: int, monday: date
    ) -> dict[str, list[dict]]:
        DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday"]
        out: dict[str, list[dict]] = {k: [] for k in DAY_KEYS}
        out["undated"] = []

        sunday = monday + timedelta(days=6)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM tasks
                   WHERE user_id = ? AND completed = 0
                     AND (
                        (due_date BETWEEN ? AND ?)
                        OR due_date IS NULL
                     )
                   ORDER BY (time IS NULL), time, from_schedule DESC, id""",
                (user_id, monday.isoformat(), sunday.isoformat()),
            )
            rows = [dict(r) for r in await cur.fetchall()]

        for t in rows:
            due = t.get("due_date")
            if not due:
                out["undated"].append(t)
                continue
            try:
                d = date.fromisoformat(due)
            except Exception:
                continue
            offset = (d - monday).days
            if 0 <= offset <= 6:
                out[DAY_KEYS[offset]].append(t)
        return out

    async def replace_week_schedule_tasks(
        self, user_id: int, monday: date, items: list[dict],
        save_snapshot: bool = True,
    ) -> tuple[int, int]:
        """Атомарно заменяет плановые задачи недели. save_snapshot=False используется undo, чтобы не затирать точку отката."""
        DAY_IDX = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                   "friday": 4, "saturday": 5, "sunday": 6}
        IDX_DAY = {v: k for k, v in DAY_IDX.items()}
        sunday = monday + timedelta(days=6)
        monday_iso = monday.isoformat()

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row

            if save_snapshot:
                cur = await db.execute(
                    """SELECT title, description, due_date, time FROM tasks
                       WHERE user_id = ? AND from_schedule = 1 AND completed = 0
                         AND due_date BETWEEN ? AND ?""",
                    (user_id, monday_iso, sunday.isoformat()),
                )
                prev_tasks = [dict(r) for r in await cur.fetchall()]
                prev_snap: dict = {k: [] for k in DAY_IDX}
                for t in prev_tasks:
                    try:
                        offset = (date.fromisoformat(t["due_date"]) - monday).days
                        if 0 <= offset <= 6:
                            prev_snap[IDX_DAY[offset]].append({
                                "task": t["title"],
                                "time": t["time"],
                                "description": t["description"] or "",
                            })
                    except Exception:
                        pass
                prev_json = json.dumps(prev_snap, ensure_ascii=False)

                cur2 = await db.execute(
                    "SELECT id FROM schedules WHERE user_id = ? AND week_start = ?",
                    (user_id, monday_iso),
                )
                if await cur2.fetchone():
                    await db.execute(
                        """UPDATE schedules
                           SET previous_json = ?, schedule_json = '{}'
                           WHERE user_id = ? AND week_start = ?""",
                        (prev_json, user_id, monday_iso),
                    )
                else:
                    await db.execute(
                        """INSERT INTO schedules (user_id, week_start, schedule_json, previous_json)
                           VALUES (?, ?, '{}', ?)""",
                        (user_id, monday_iso, prev_json),
                    )
                    await db.execute(
                        """UPDATE schedules SET previous_json = ?
                           WHERE user_id = ? AND week_start = ?""",
                        (prev_json, user_id, monday_iso),
                    )

            cur = await db.execute(
                """DELETE FROM tasks
                   WHERE user_id = ? AND from_schedule = 1 AND completed = 0
                     AND due_date BETWEEN ? AND ?""",
                (user_id, monday_iso, sunday.isoformat()),
            )
            deleted_n = cur.rowcount or 0

            new_snap: dict = {k: [] for k in DAY_IDX}
            added_n = 0
            for it in items:
                day_key = (it.get("day") or "").lower()
                if day_key not in DAY_IDX:
                    continue
                title = (it.get("task") or "").strip()
                if not title:
                    continue
                due = (monday + timedelta(days=DAY_IDX[day_key])).isoformat()
                time_val = (it.get("time") or "").strip() or None
                desc = (it.get("description") or "").strip()
                await db.execute(
                    """INSERT INTO tasks
                           (user_id, title, description, due_date, time, source, from_schedule)
                       VALUES (?, ?, ?, ?, ?, 'schedule', 1)""",
                    (user_id, title, desc, due, time_val),
                )
                new_snap[day_key].append({"task": title, "time": time_val, "description": desc})
                added_n += 1

            if save_snapshot:
                await db.execute(
                    """UPDATE schedules SET schedule_json = ?
                       WHERE user_id = ? AND week_start = ?""",
                    (json.dumps(new_snap, ensure_ascii=False), user_id, monday_iso),
                )

            await db.commit()
        return deleted_n, added_n

    async def get_tasks(
        self,
        user_id: int,
        include_completed: bool = False,
        due_before: Optional[str] = None,
    ) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM tasks WHERE user_id = ?"
            params: list = [user_id]
            if not include_completed:
                query += " AND completed = 0"
            if due_before:
                query += " AND (due_date IS NULL OR due_date <= ?)"
                params.append(due_before)
            query += (
                " ORDER BY"
                " CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 4 ELSE 3 END,"
                " CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,"
                " due_date ASC, created_at ASC"
            )
            cur = await db.execute(query, params)
            return [dict(r) for r in await cur.fetchall()]

    async def complete_task(self, task_id: int, user_id: int) -> Optional[dict]:
        """Для recurring-задач не ставит completed=1 (иначе задача пропадёт навсегда) — только логирует выполнение."""
        today = date.today().isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )
            row = await cur.fetchone()
            if not row:
                return None
            task = dict(row)

            if not task.get("recurring") and task.get("completed"):
                return None

            if task.get("recurring"):
                cur = await db.execute(
                    "SELECT 1 FROM completions WHERE task_id = ? AND completed_on = ?",
                    (task_id, today),
                )
                already_today = await cur.fetchone()
                if already_today:
                    return task  # idempotent

            if not task.get("recurring"):
                await db.execute(
                    "UPDATE tasks SET completed = 1, completed_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), task_id),
                )

            await db.execute(
                "INSERT INTO completions (user_id, task_id, completed_on) VALUES (?, ?, ?)",
                (user_id, task_id, today),
            )
            await db.commit()

        # Запись completion нужна для daily_stats даже без отображения стриков.
        return task

    async def find_active_task_by_title_exact(
        self, user_id: int, title: str
    ) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM tasks
                   WHERE user_id = ? AND completed = 0
                     AND LOWER(title) = LOWER(?)
                   ORDER BY recurring IS NULL, id DESC
                   LIMIT 1""",
                (user_id, title.strip()),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def mark_schedule_items_done_by_title(
        self, user_id: int, title: str, day_key: Optional[str] = None
    ) -> int:
        monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM schedules WHERE user_id = ? AND week_start = ?",
                (user_id, monday),
            )
            row = await cur.fetchone()
            if not row:
                return 0
            schedule = json.loads(row["schedule_json"])

            target_keys = [day_key] if day_key else list(schedule.keys())
            tlow = title.strip().lower()
            touched = 0
            for key in target_keys:
                items = schedule.get(key, []) or []
                for item in items:
                    if (item.get("task") or "").strip().lower() == tlow and not item.get("done"):
                        item["done"] = True
                        touched += 1

            if touched:
                await db.execute(
                    "UPDATE schedules SET schedule_json = ? WHERE user_id = ? AND week_start = ?",
                    (json.dumps(schedule, ensure_ascii=False), user_id, monday),
                )
                await db.commit()
            return touched

    async def delete_task(self, task_id: int, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )
            row = await cur.fetchone()
            if not row:
                return None
            task = dict(row)
            await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            await db.commit()
            return task

    async def get_task_by_id(self, task_id: int, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_task_time(
        self, task_id: int, user_id: int, time: Optional[str]
    ) -> Optional[dict]:
        """Сбрасывает напоминание при изменении времени — оно было рассчитано относительно старого."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
            )
            if not await cur.fetchone():
                return None
            await db.execute(
                """UPDATE tasks
                   SET time = ?, notify_before = NULL, remind_at = NULL, reminded = 0
                   WHERE id = ?""",
                (time, task_id),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def find_tasks_by_title(self, user_id: int, query: str, limit: int = 5) -> list[dict]:
        # lower_py вместо SQLite LOWER() — встроенный не обрабатывает кириллицу.
        q_lower = query.strip().lower()
        async with aiosqlite.connect(self.path) as db:
            await db.create_function("lower_py", 1, str.lower)
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT *,
                     CASE
                       WHEN lower_py(title) = ?    THEN 0
                       WHEN lower_py(title) LIKE ? THEN 1
                       ELSE                             2
                     END AS match_rank
                   FROM tasks
                   WHERE user_id = ? AND completed = 0
                     AND lower_py(title) LIKE ?
                   ORDER BY match_rank, due_date ASC NULLS LAST
                   LIMIT ?""",
                (q_lower, f"{q_lower}%", user_id, f"%{q_lower}%", limit),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def set_task_priority(
        self, task_id: int, user_id: int, priority: Optional[str]
    ) -> Optional[dict]:
        valid_priorities = {"high", "medium", "low", None}
        if priority not in valid_priorities:
            priority = None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )
            row = await cur.fetchone()
            if not row:
                return None
            await db.execute(
                "UPDATE tasks SET priority = ? WHERE id = ?",
                (priority, task_id),
            )
            await db.commit()
            cur = await db.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_overdue_tasks(self, user_id: int) -> list[dict]:
        today = date.today().isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM tasks
                   WHERE user_id = ? AND completed = 0
                     AND due_date IS NOT NULL AND due_date < ?
                   ORDER BY due_date ASC""",
                (user_id, today),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_upcoming_tasks(self, user_id: int, days: int = 1) -> list[dict]:
        today = date.today().isoformat()
        until = (date.today() + timedelta(days=days)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM tasks
                   WHERE user_id = ? AND completed = 0
                     AND due_date IS NOT NULL
                     AND due_date >= ? AND due_date <= ?
                   ORDER BY due_date ASC""",
                (user_id, today, until),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def set_task_reminder(
        self, task_id: int, user_id: int, notify_before: Optional[int]
    ) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )
            row = await cur.fetchone()
            if not row:
                return None
            task = dict(row)

            if notify_before is None:
                await db.execute(
                    "UPDATE tasks SET notify_before = NULL, remind_at = NULL, reminded = 0 WHERE id = ?",
                    (task_id,),
                )
                await db.commit()
                task.update({"notify_before": None, "remind_at": None, "reminded": 0})
                return task

            if not task.get("time"):
                return None

            h, m = map(int, task["time"].split(":"))
            total = h * 60 + m - notify_before
            if total < 0:
                return None

            remind_h, remind_m = divmod(total, 60)
            remind_at = f"{remind_h:02d}:{remind_m:02d}"
            await db.execute(
                "UPDATE tasks SET notify_before = ?, remind_at = ?, reminded = 0 WHERE id = ?",
                (notify_before, remind_at, task_id),
            )
            await db.commit()
            task.update({"notify_before": notify_before, "remind_at": remind_at, "reminded": 0})
            return task

    async def get_tasks_to_remind(
        self, user_id: int, remind_at_hhmm: str, today_date: str
    ) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM tasks
                   WHERE user_id = ? AND remind_at = ? AND due_date = ?
                     AND reminded = 0 AND completed = 0""",
                (user_id, remind_at_hhmm, today_date),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def mark_tasks_reminded(self, task_ids: list[int]) -> None:
        if not task_ids:
            return
        placeholders = ",".join("?" * len(task_ids))
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE tasks SET reminded = 1 WHERE id IN ({placeholders})",
                task_ids,
            )
            await db.commit()

    async def get_notification_settings(self, telegram_id: int) -> dict:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT notify_morning, notify_evening, notify_reminders, notify_weather FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cur.fetchone()
            if not row:
                return {"morning": "08:00", "evening": "21:00", "reminders": "12:00,17:00", "weather": False}
            return {
                "morning": row[0] or None,
                "evening": row[1] or None,
                "reminders": row[2] or None,
                "weather": bool(row[3]),
            }

    async def set_weather_notification(self, telegram_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET notify_weather = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            await db.commit()

    async def set_notification_settings(
        self,
        telegram_id: int,
        morning: Optional[str],
        evening: Optional[str],
        reminders: Optional[str],
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE users
                   SET notify_morning = ?, notify_evening = ?, notify_reminders = ?
                   WHERE telegram_id = ?""",
                (morning, evening, reminders, telegram_id),
            )
            await db.commit()

    async def get_location(self, telegram_id: int) -> dict:
        """Вернуть {"city": str|None, "lat": float|None, "lon": float|None}."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT city, lat, lon FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cur.fetchone()
            if not row:
                return {"city": None, "lat": None, "lon": None}
            return {"city": row[0], "lat": row[1], "lon": row[2]}

    async def set_location(
        self,
        telegram_id: int,
        *,
        city: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET city = ?, lat = ?, lon = ? WHERE telegram_id = ?",
                (city, lat, lon, telegram_id),
            )
            await db.commit()

    async def save_schedule(
        self,
        user_id: int,
        week_start: str,
        schedule: dict,
        *,
        keep_history: bool = False,
    ) -> None:
        """keep_history=True сохраняет текущее расписание как previous_json перед перезаписью."""
        new_json = json.dumps(schedule, ensure_ascii=False)
        async with aiosqlite.connect(self.path) as db:
            if keep_history:
                cur = await db.execute(
                    "SELECT schedule_json FROM schedules WHERE user_id = ? AND week_start = ?",
                    (user_id, week_start),
                )
                row = await cur.fetchone()
                prev_json = row[0] if row else None
                await db.execute(
                    """INSERT INTO schedules (user_id, week_start, schedule_json, previous_json)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id, week_start) DO UPDATE SET
                           schedule_json = excluded.schedule_json,
                           previous_json = excluded.previous_json""",
                    (user_id, week_start, new_json, prev_json),
                )
            else:
                await db.execute(
                    """INSERT INTO schedules (user_id, week_start, schedule_json)
                       VALUES (?, ?, ?)
                       ON CONFLICT(user_id, week_start) DO UPDATE SET
                           schedule_json = excluded.schedule_json""",
                    (user_id, week_start, new_json),
                )
            await db.commit()

    async def restore_schedule_previous(self, user_id: int, week_start: str) -> Optional[dict]:
        """Swap schedule_json ↔ previous_json (поддерживает «качели» — повторный undo возвращает обратно)."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM schedules WHERE user_id = ? AND week_start = ?",
                (user_id, week_start),
            )
            row = await cur.fetchone()
            if not row or not row["previous_json"]:
                return None
            current = row["schedule_json"]
            previous = row["previous_json"]
            await db.execute(
                """UPDATE schedules SET schedule_json = ?, previous_json = ?
                   WHERE user_id = ? AND week_start = ?""",
                (previous, current, user_id, week_start),
            )
            await db.commit()
            return json.loads(previous)

    async def get_current_schedule(self, user_id: int) -> Optional[dict]:
        monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM schedules WHERE user_id = ? AND week_start = ?",
                (user_id, monday),
            )
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d["schedule"] = json.loads(d["schedule_json"])
            return d

    async def _update_streak(self, task_id: int, user_id: int, today: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM streaks WHERE task_id = ? AND user_id = ?",
                (task_id, user_id),
            )
            row = await cur.fetchone()

            if row is None:
                await db.execute(
                    """INSERT INTO streaks (user_id, task_id, current_streak, longest_streak, last_completed)
                       VALUES (?, ?, 1, 1, ?)""",
                    (user_id, task_id, today),
                )
            else:
                streak = dict(row)
                last = streak["last_completed"]
                current = streak["current_streak"]
                longest = streak["longest_streak"]

                if last:
                    last_date = date.fromisoformat(last)
                    today_date = date.fromisoformat(today)
                    diff = (today_date - last_date).days
                    if diff == 1:
                        current += 1
                    elif diff == 0:
                        pass  # duplicate completion same day
                    else:
                        current = 1  # streak broken
                else:
                    current = 1

                longest = max(longest, current)
                await db.execute(
                    """UPDATE streaks SET current_streak = ?, longest_streak = ?, last_completed = ?
                       WHERE task_id = ? AND user_id = ?""",
                    (current, longest, today, task_id, user_id),
                )
            await db.commit()

    async def get_all_streaks(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT s.*, t.title FROM streaks s
                   JOIN tasks t ON t.id = s.task_id
                   WHERE s.user_id = ? AND s.current_streak > 0
                   ORDER BY s.current_streak DESC""",
                (user_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_streak_for_task(self, task_id: int, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM streaks WHERE task_id = ? AND user_id = ?",
                (task_id, user_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def add_diary_entry(
        self,
        user_id: int,
        content: str,
        entry_type: str = "observation",
        importance: int = 5,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO diary (user_id, content, entry_type, importance)
                   VALUES (?, ?, ?, ?)""",
                (user_id, content, entry_type, importance),
            )
            await db.commit()

    async def get_recent_diary(self, user_id: int, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM diary WHERE user_id = ?
                   ORDER BY importance DESC, created_at DESC
                   LIMIT ?""",
                (user_id, limit),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_user_summary_context(self, telegram_id: int) -> str:
        """Строит контекст из дневника/задач/заметок для инъекции в AI-промпты."""
        db_user = await self.get_user_by_telegram_id(telegram_id)
        if not db_user:
            return ""
        user_id = db_user["id"]

        today = date.today().isoformat()
        sunday = (date.today() + timedelta(days=6 - date.today().weekday())).isoformat()

        # Задачи на сегодня — явный список, чтобы ИИ не делал вывод сам
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM tasks
                   WHERE user_id = ? AND completed = 0 AND due_date = ?
                   ORDER BY (time IS NULL), time, from_schedule DESC, id""",
                (user_id, today),
            )
            today_tasks = [dict(r) for r in await cur.fetchall()]

        # Задачи на остаток текущей недели (после сегодня)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM tasks
                   WHERE user_id = ? AND completed = 0
                     AND due_date > ? AND due_date <= ?
                   ORDER BY due_date ASC, (time IS NULL), time, id""",
                (user_id, today, sunday),
            )
            week_tasks = [dict(r) for r in await cur.fetchall()]

        diary = await self.get_recent_diary(user_id, limit=8)
        tasks = await self.get_tasks(user_id)
        overdue = await self.get_overdue_tasks(user_id)
        streaks = await self.get_all_streaks(user_id)
        notes = await self.get_notes(user_id, limit=5)

        parts: list[str] = []

        if today_tasks:
            lines = []
            for t in today_tasks:
                time_part = f"{t['time']} " if t.get("time") else ""
                lines.append(f"- {time_part}{t['title']}")
            parts.append(f"Планы на сегодня ({today}):\n" + "\n".join(lines))
        else:
            parts.append(f"Планы на сегодня ({today}): нет запланированных задач")

        if week_tasks:
            _DAY_RU = {
                0: "Понедельник", 1: "Вторник", 2: "Среда", 3: "Четверг",
                4: "Пятница", 5: "Суббота", 6: "Воскресенье",
            }
            from collections import defaultdict
            by_day: dict[str, list] = defaultdict(list)
            for t in week_tasks:
                by_day[t["due_date"]].append(t)
            lines = []
            for day_iso in sorted(by_day.keys()):
                d = date.fromisoformat(day_iso)
                day_label = f"{_DAY_RU[d.weekday()]} {d.strftime('%d.%m')}"
                for t in by_day[day_iso]:
                    time_part = f"{t['time']} " if t.get("time") else ""
                    lines.append(f"- {day_label}: {time_part}{t['title']}")
            parts.append("Планы на неделю:\n" + "\n".join(lines))

        if tasks:
            task_text = "\n".join(
                f"- [id={t['id']}] {t['title']}"
                + (f" ⏰{t['time']}" if t.get("time") else "")
                + (f" 🔔-{t['notify_before']}м" if t.get("notify_before") else "")
                + (f" (до {t['due_date']})" if t["due_date"] else "")
                for t in tasks[:20]
            )
            parts.append(f"Активные задачи (с ID):\n{task_text}")

        if overdue:
            overdue_text = "\n".join(f"- {t['title']} (просрочено {t['due_date']})" for t in overdue)
            parts.append(f"Просроченные задачи:\n{overdue_text}")

        if streaks:
            streak_text = "\n".join(f"- {s['title']}: {s['current_streak']} дней подряд 🔥" for s in streaks[:5])
            parts.append(f"Текущие серии:\n{streak_text}")

        if diary:
            diary_text = "\n".join(f"- [{e['entry_type']}] {e['content']}" for e in diary)
            parts.append(f"Дневник наблюдений:\n{diary_text}")

        if notes:
            notes_text = "\n".join(f"- [id={n['id']}] {n['content']}" for n in notes)
            parts.append(f"Последние заметки (с ID):\n{notes_text}")

        return "\n\n".join(parts)

    async def get_completion_stats(self, user_id: int, days: int = 7) -> dict:
        since = (date.today() - timedelta(days=days)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """SELECT COUNT(*) as total FROM completions
                   WHERE user_id = ? AND completed_on >= ?""",
                (user_id, since),
            )
            row = await cur.fetchone()
            total = row[0] if row else 0

        return {"period_days": days, "completions": total}

    async def daily_stats(self, user_id: int, day: date) -> dict:
        day_iso = day.isoformat()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE user_id = ?
                     AND (
                        due_date = ?
                        OR (recurring IS NOT NULL AND DATE(created_at) <= ?)
                     )""",
                (user_id, day_iso, day_iso),
            )
            planned = (await cur.fetchone())[0] or 0

            # Просроченные задачи не влияют на rate — они не были в плане на этот день.
            cur = await db.execute(
                """SELECT COUNT(*) FROM completions c
                   JOIN tasks t ON t.id = c.task_id
                   WHERE c.user_id = ? AND c.completed_on = ?
                     AND (t.due_date = ? OR t.recurring IS NOT NULL)""",
                (user_id, day_iso, day_iso),
            )
            completed = (await cur.fetchone())[0] or 0

        if planned == 0 and completed == 0:
            rate = None
        elif planned == 0:
            rate = 1.0  # сделал что-то «вне плана» — это всё равно продуктивно
        else:
            rate = min(1.0, completed / planned)

        return {
            "day": day_iso,
            "planned": planned,
            "completed": completed,
            "rate": rate,
        }

    async def daily_stats_range(
        self, user_id: int, start: date, end: date
    ) -> list[dict]:
        out: list[dict] = []
        cur = start
        while cur <= end:
            out.append(await self.daily_stats(user_id, cur))
            cur += timedelta(days=1)
        return out

    async def add_note(self, user_id: int, content: str, source: str = "manual") -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO notes (user_id, content, source) VALUES (?, ?, ?)",
                (user_id, content.strip(), source),
            )
            await db.commit()
            return cur.lastrowid

    async def get_notes(self, user_id: int, limit: int = 15) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM notes WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def delete_note(self, note_id: int, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM notes WHERE id = ? AND user_id = ?",
                (note_id, user_id),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0
