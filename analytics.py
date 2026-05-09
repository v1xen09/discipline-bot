"""
Рисование графиков продуктивности через matplotlib.

Стратегия:
  • Backend «Agg» — не требует GUI и работает на Windows из-под службы.
  • Все графики рендерятся в BytesIO и отдаются как PNG-байты;
    handler'ы передают их в bot.send_photo(...).
  • Цветовая шкала RdYlGn (красный → жёлтый → зелёный) — чтобы плохой день
    сразу читался глазом.
"""

from __future__ import annotations

import calendar
import io
from datetime import date

import matplotlib

matplotlib.use("Agg")  # без GUI; ВАЖНО — до import pyplot

import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib import colormaps

DAY_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTH_NAMES_RU = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def _rate_color(rate: float | None):
    """Цвет квадратика/столбика по коэффициенту 0..1.
    None → серый (нет данных)."""
    if rate is None:
        return (0.85, 0.85, 0.85, 1.0)
    cmap = colormaps["RdYlGn"]
    return cmap(max(0.0, min(1.0, rate)))


def _save_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def render_week_chart(stats: list[dict], today: date) -> bytes:
    """
    7 столбиков с днями недели. Высота — процент выполнения.
    Пунктирная горизонтальная линия на 50%.
    Дни без планов рисуются серым штрихом.

    stats: список из ровно 7 элементов — Пн..Вс — каждый dict с ключами
           rate (None|0..1), planned, completed.
    """
    fig, ax = plt.subplots(figsize=(8, 4.2))

    labels = []
    heights = []
    colors = []
    annotations = []

    for i, st in enumerate(stats):
        rate = st.get("rate")
        planned = st.get("planned", 0)
        completed = st.get("completed", 0)

        # Метка дня + дата
        try:
            d = date.fromisoformat(st["day"])
            labels.append(f"{DAY_SHORT_RU[d.weekday()]}\n{d.strftime('%d.%m')}")
        except Exception:
            labels.append(DAY_SHORT_RU[i % 7])

        if rate is None:
            heights.append(0)
            colors.append((0.9, 0.9, 0.9, 1.0))
            annotations.append("—")
        else:
            heights.append(rate * 100)
            colors.append(_rate_color(rate))
            annotations.append(f"{completed}/{planned}\n{int(round(rate * 100))}%")

    bars = ax.bar(labels, heights, color=colors, edgecolor="#444", linewidth=0.6)

    # Пунктирная отсечка 50%
    ax.axhline(50, linestyle="--", linewidth=1.2, color="#888", alpha=0.7)
    ax.text(len(stats) - 0.5, 51, "50%", color="#666", fontsize=8,
            ha="right", va="bottom")

    ax.set_ylim(0, 105)
    ax.set_ylabel("Выполнение, %")
    ax.set_title("Продуктивность за неделю", fontsize=13, pad=10)
    ax.grid(axis="y", linestyle=":", alpha=0.35)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    # Подписи над/в столбиках
    for bar, ann in zip(bars, annotations):
        h = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2, h + 2 if h > 0 else 2,
            ann, ha="center", va="bottom", fontsize=8.5, color="#333",
        )

    # Подсветка «сегодня»
    today_label_idx = None
    for i, st in enumerate(stats):
        if st.get("day") == today.isoformat():
            today_label_idx = i
            break
    if today_label_idx is not None:
        ticks = ax.get_xticklabels()
        if today_label_idx < len(ticks):
            ticks[today_label_idx].set_fontweight("bold")
            ticks[today_label_idx].set_color("#1a73e8")

    return _save_png(fig)


def render_month_chart(stats: list[dict], today: date) -> bytes:
    """
    Календарная сетка 6×7 за месяц today. Каждый день — квадрат, цвет
    по rate. Дни вне месяца — пустые. Сегодня обведён жирно.
    """
    year = today.year
    month = today.month
    cal = calendar.Calendar(firstweekday=0)  # Пн = 0

    # day_to_stat: 'YYYY-MM-DD' -> stat
    day_to_stat = {s["day"]: s for s in stats}

    weeks = list(cal.monthdayscalendar(year, month))  # 6×7 матрица, 0 = вне месяца

    fig, ax = plt.subplots(figsize=(7, 5.2))
    ax.set_xlim(0, 7)
    ax.set_ylim(0, len(weeks))
    ax.invert_yaxis()
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Заголовки дней недели
    for i, lbl in enumerate(DAY_SHORT_RU):
        ax.text(i + 0.5, -0.55, lbl, ha="center", va="bottom",
                fontsize=10, color="#555", fontweight="bold")

    pad = 0.06
    today_iso = today.isoformat()

    for row, week in enumerate(weeks):
        for col, day in enumerate(week):
            if day == 0:
                continue
            iso = date(year, month, day).isoformat()
            stat = day_to_stat.get(iso)
            rate = stat.get("rate") if stat else None
            color = _rate_color(rate)

            # Квадратик
            rect = patches.Rectangle(
                (col + pad, row + pad),
                1 - 2 * pad, 1 - 2 * pad,
                facecolor=color,
                edgecolor="#333" if iso == today_iso else "#bbb",
                linewidth=2.2 if iso == today_iso else 0.6,
            )
            ax.add_patch(rect)

            # Номер дня
            text_color = "#222" if rate is not None and rate > 0.3 else "#444"
            ax.text(
                col + 0.5, row + 0.32, f"{day}",
                ha="center", va="center",
                fontsize=10, color=text_color, fontweight="bold",
            )
            # Процент мелким шрифтом, если есть
            if rate is not None:
                ax.text(
                    col + 0.5, row + 0.7, f"{int(round(rate * 100))}%",
                    ha="center", va="center",
                    fontsize=8, color="#222",
                )

    ax.set_title(
        f"Продуктивность · {MONTH_NAMES_RU[month]} {year}",
        fontsize=13, pad=42,
    )
    return _save_png(fig)


def render_today_chart(stat: dict) -> bytes:
    """Простой горизонтальный «прогресс-бар» для итога дня."""
    rate = stat.get("rate")
    planned = stat.get("planned", 0)
    completed = stat.get("completed", 0)

    fig, ax = plt.subplots(figsize=(7, 1.6))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # Фоновая шкала
    ax.add_patch(patches.Rectangle(
        (0.02, 0.35), 0.96, 0.3,
        facecolor="#eee", edgecolor="#bbb", linewidth=0.6,
    ))
    if rate is not None and rate > 0:
        ax.add_patch(patches.Rectangle(
            (0.02, 0.35), 0.96 * rate, 0.3,
            facecolor=_rate_color(rate), edgecolor="#333", linewidth=0.6,
        ))
    # Отметка 50%
    ax.plot([0.02 + 0.96 * 0.5, 0.02 + 0.96 * 0.5], [0.30, 0.70],
            linestyle="--", color="#888", linewidth=1)
    ax.text(0.02 + 0.96 * 0.5, 0.74, "50%", ha="center", va="bottom",
            fontsize=8, color="#666")

    # Подписи
    pct = "—" if rate is None else f"{int(round(rate * 100))}%"
    ax.text(0.5, 0.10,
            f"{completed} из {planned} · {pct}",
            ha="center", va="center", fontsize=12, fontweight="bold")
    return _save_png(fig)
