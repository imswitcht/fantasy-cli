"""Terminal output.

Uses `rich` when it's installed (colored tables, which is what you want on a
Sunday morning) and falls back to plain ASCII when it isn't. Keeping rich
optional means the tool still runs on a machine where you haven't installed
anything but requests.
"""
from __future__ import annotations

import re
import shutil
from typing import Any, Optional

try:  # pragma: no cover - exercised by whichever branch the machine has
    from rich.console import Console as _RichConsole
    from rich.panel import Panel as _RichPanel
    from rich.table import Table as _RichTable
    from rich.text import Text as _RichText
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False

_TAG = re.compile(r"\[/?[a-zA-Z0-9 _#=.]+\]")


def strip_markup(s: Any) -> str:
    return _TAG.sub("", str(s))


# --------------------------------------------------------------- fallbacks

class _PlainText:
    def __init__(self, text: str = "", style: str = ""):
        self.text = str(text)
        self.style = style

    def __str__(self) -> str:
        return self.text

    def __len__(self) -> int:
        return len(self.text)


class _PlainTable:
    def __init__(self, title: Optional[str] = None, **kw):
        self.title = strip_markup(title) if title else None
        self.columns: list[str] = []
        self.rows: list[list[str]] = []

    def add_column(self, header: str = "", **kw):
        self.columns.append(strip_markup(header))

    def add_row(self, *cells):
        self.rows.append([strip_markup(c) for c in cells])

    def render(self) -> str:
        width = shutil.get_terminal_size((100, 24)).columns
        widths = [len(c) for c in self.columns]
        for row in self.rows:
            for i, cell in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(cell))
        # Keep the whole thing inside the terminal.
        total = sum(widths) + 2 * (len(widths) - 1)
        if total > width and widths:
            overflow = total - width
            biggest = widths.index(max(widths))
            widths[biggest] = max(8, widths[biggest] - overflow)

        def fmt(cells):
            out = []
            for i, c in enumerate(cells):
                w = widths[i] if i < len(widths) else len(c)
                out.append(c[:w].ljust(w))
            return "  ".join(out).rstrip()

        lines = []
        if self.title:
            lines.append(self.title)
        if self.columns:
            lines.append(fmt(self.columns))
            lines.append("-" * min(sum(widths) + 2 * (len(widths) - 1), width))
        lines.extend(fmt(r) for r in self.rows)
        return "\n".join(lines)


class _PlainPanel:
    def __init__(self, content: Any, **kw):
        self.content = strip_markup(content)

    @classmethod
    def fit(cls, content: Any, **kw):
        return cls(content)

    def render(self) -> str:
        lines = self.content.splitlines() or [""]
        width = min(max(len(l) for l in lines) + 2,
                    shutil.get_terminal_size((100, 24)).columns - 2)
        top = "+" + "-" * width + "+"
        body = "\n".join("| " + l[:width - 2].ljust(width - 2) + " |" for l in lines)
        return f"{top}\n{body}\n{top}"


class _PlainConsole:
    def print(self, *args, **kw):
        if not args:
            print()
            return
        for a in args:
            if isinstance(a, (_PlainTable, _PlainPanel)):
                print(a.render())
            else:
                print(strip_markup(a))


# ------------------------------------------------------------------ exports

if HAVE_RICH:
    Console = _RichConsole
    Table = _RichTable
    Panel = _RichPanel
    Text = _RichText
else:
    Console = _PlainConsole
    Table = _PlainTable
    Panel = _PlainPanel
    Text = _PlainText

console = Console()
