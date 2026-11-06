"""A small two-series line chart on a Tk canvas, with a hover crosshair."""
from __future__ import annotations

import math
import tkinter as tk

from .common import FONT_SMALL, colors

LEFT, RIGHT, TOP, BOTTOM = 78, 132, 34, 30


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """At least two distinct ticks that cover low..high, even for a perfectly flat line."""
    if high <= low:  # a new account sits exactly at its starting balance
        pad = max(abs(high) * 0.01, 1.0)
        low, high = low - pad, high + pad
    raw = (high - low) / max(count - 1, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    value = math.floor(low / step) * step
    ticks = [round(value, 6)]
    while value < high:
        value += step
        ticks.append(round(value, 6))
    return ticks


class ValueChart(tk.Canvas):
    """Account value over matching dates. Both series share one axis and one currency."""

    def __init__(self, parent):
        super().__init__(parent, background=colors()["surface"], highlightthickness=0, height=300)
        self.dates: list[str] = []
        self.my: list[float] = []
        self.ai: list[float] = []
        self.baseline: float | None = None
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda _e: self.delete("hover"))

    def retheme(self) -> None:
        self.configure(background=colors()["surface"])
        self.redraw()

    def set_data(self, dates, my, ai, baseline) -> None:
        self.dates, self.my, self.ai, self.baseline = dates, my, ai, baseline
        self.redraw()

    def _x(self, i: int) -> float:
        width = self.winfo_width() - LEFT - RIGHT
        return LEFT + (width * i / (len(self.dates) - 1) if len(self.dates) > 1 else width / 2)

    def _y(self, value: float) -> float:
        height = self.winfo_height() - TOP - BOTTOM
        return TOP + height * (1 - (value - self.low) / (self.high - self.low))

    def redraw(self) -> None:
        self.delete("all")
        c = colors()
        if not self.dates:
            self.create_text(self.winfo_width() / 2, self.winfo_height() / 2, fill=c["muted"],
                             font=FONT_SMALL, text="No snapshots yet. The chart fills in over time.")
            return
        values = self.my + self.ai + ([self.baseline] if self.baseline else [])
        ticks = nice_ticks(min(values), max(values))
        self.low, self.high = ticks[0], ticks[-1]
        right = self.winfo_width() - RIGHT
        for tick in ticks:
            y = self._y(tick)
            self.create_line(LEFT, y, right, y, fill=c["grid"])
            self.create_text(LEFT - 8, y, text=f"{tick:,.0f}", anchor="e", fill=c["muted"],
                             font=FONT_SMALL)
        self.create_text(LEFT - 8, 12, text="CAD", anchor="e", fill=c["muted"], font=FONT_SMALL)

        indexes = {0, len(self.dates) // 2, len(self.dates) - 1}
        for i in sorted(indexes):
            anchor = "n" if 0 < i < len(self.dates) - 1 else ("nw" if i == 0 else "ne")
            self.create_text(self._x(i), self.winfo_height() - BOTTOM + 6, text=self.dates[i],
                             anchor=anchor, fill=c["muted"], font=FONT_SMALL)

        if self.baseline:
            y = self._y(self.baseline)
            self.create_line(LEFT, y, right, y, fill=c["muted"], dash=(2, 4))

        self._series(self.my, c["my"], None)
        self._series(self.ai, c["ai"], (7, 4))
        self._end_labels()
        self._legend()

    def _series(self, values, color, dash) -> None:
        points = [p for i, v in enumerate(values) for p in (self._x(i), self._y(v))]
        if len(points) >= 4:
            self.create_line(*points, fill=color, width=2, dash=dash, joinstyle="round")
        x, y = self._x(len(values) - 1), self._y(values[-1])
        self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline=colors()["surface"], width=2)

    def _end_labels(self) -> None:
        c = colors()
        y_my, y_ai = self._y(self.my[-1]), self._y(self.ai[-1])
        if abs(y_my - y_ai) < 34:  # keep the two labels from colliding
            middle = (y_my + y_ai) / 2
            offset = 17 if y_my >= y_ai else -17
            y_my, y_ai = middle + offset, middle - offset
        x = self.winfo_width() - RIGHT + 10
        for y, name, value in ((y_my, "My account", self.my[-1]), (y_ai, "AI account", self.ai[-1])):
            self.create_text(x, y - 8, text=name, anchor="w", fill=c["muted"], font=FONT_SMALL)
            self.create_text(x, y + 8, text=f"{value:,.2f}", anchor="w", fill=c["ink"], font=FONT_SMALL)

    def _legend(self) -> None:
        c = colors()
        x = LEFT
        for name, color, dash in (("My account", c["my"], None), ("AI account", c["ai"], (7, 4))):
            self.create_line(x, 14, x + 26, 14, fill=color, width=2, dash=dash)
            self.create_text(x + 32, 14, text=name, anchor="w", fill=c["ink"], font=FONT_SMALL)
            x += 118
        self.create_line(x, 14, x + 26, 14, fill=c["muted"], dash=(2, 4))
        self.create_text(x + 32, 14, text="Starting balance", anchor="w", fill=c["ink"], font=FONT_SMALL)

    def _hover(self, event) -> None:
        self.delete("hover")
        if not self.dates or not LEFT <= event.x <= self.winfo_width() - RIGHT:
            return
        c = colors()
        width = self.winfo_width() - LEFT - RIGHT
        i = round((event.x - LEFT) / width * (len(self.dates) - 1)) if len(self.dates) > 1 else 0
        i = min(max(i, 0), len(self.dates) - 1)
        x = self._x(i)
        self.create_line(x, TOP, x, self.winfo_height() - BOTTOM, fill=c["muted"], tags="hover")
        text = f"{self.dates[i]}\nMy account  {self.my[i]:,.2f}\nAI account  {self.ai[i]:,.2f}"
        box_x = x + 10 if x + 190 < self.winfo_width() else x - 180
        label = self.create_text(box_x + 8, TOP + 10, text=text, anchor="nw", fill=c["ink"],
                                 font=FONT_SMALL, tags="hover")
        x0, y0, x1, y1 = self.bbox(label)
        back = self.create_rectangle(x0 - 8, y0 - 6, x1 + 8, y1 + 6, fill=c["surface"],
                                     outline=c["grid"], tags="hover")
        self.tag_lower(back, label)
