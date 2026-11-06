"""Shared UI pieces and the light and dark themes."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 9)
FONT_BIG = ("Segoe UI", 15, "bold")
PAPER_BANNER = "#7a1f1f"
DEMO_BANNER = "#6b4e00"

LIGHT = {
    "bg": "#f0efeb", "surface": "#fcfcfb", "field": "#ffffff", "panel": "#e9e8e3",
    "ink": "#1f1f1e", "muted": "#5a5956", "disabled": "#9a9994", "grid": "#e4e3df",
    "border": "#c8c7c2", "select": "#cfe1f7", "button": "#e7e6e1", "button_active": "#dcdbd6",
    "warn": "#8a4b00", "my": "#2a78d6", "ai": "#eb6834",
}
DARK = {
    "bg": "#1f1f1e", "surface": "#1a1a19", "field": "#2a2a28", "panel": "#262624",
    "ink": "#f0efec", "muted": "#c3c2b7", "disabled": "#7d7c76", "grid": "#3a3a37",
    "border": "#4a4a46", "select": "#2f5f9e", "button": "#33332f", "button_active": "#42423d",
    "warn": "#f0b35c", "my": "#3987e5", "ai": "#d95926",
}
_palette = DARK


def colors() -> dict:
    return _palette


def _style(root: tk.Misc, c: dict) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")  # the native Windows theme ignores most colour settings
    style.configure(".", font=FONT, background=c["bg"], foreground=c["ink"],
                    fieldbackground=c["field"], bordercolor=c["border"], lightcolor=c["bg"],
                    darkcolor=c["bg"], troughcolor=c["panel"], focuscolor=c["select"],
                    insertcolor=c["ink"], selectbackground=c["select"], selectforeground=c["ink"])
    style.map(".", foreground=[("disabled", c["disabled"])])
    style.configure("TButton", background=c["button"], padding=(8, 3))
    style.map("TButton", background=[("disabled", c["bg"]), ("active", c["button_active"])],
              foreground=[("disabled", c["disabled"])])
    style.configure("TEntry", fieldbackground=c["field"], lightcolor=c["field"], darkcolor=c["field"])
    style.configure("TCombobox", fieldbackground=c["field"], background=c["button"],
                    arrowcolor=c["ink"], lightcolor=c["field"], darkcolor=c["field"])
    style.map("TCombobox", fieldbackground=[("readonly", c["field"])],
              foreground=[("readonly", c["ink"])], background=[("active", c["button_active"])],
              selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["ink"])])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, indicatorbackground=c["field"], indicatorforeground=c["ink"],
                        upperbordercolor=c["border"], lowerbordercolor=c["border"])
        style.map(name, background=[("active", c["bg"])],
                  indicatorbackground=[("selected", c["my"]), ("pressed", c["select"])])
    style.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
    style.configure("TNotebook.Tab", background=c["button"], foreground=c["ink"], padding=(10, 4))
    style.map("TNotebook.Tab", background=[("selected", c["bg"]), ("active", c["button_active"])],
              lightcolor=[("selected", c["bg"])])
    style.configure("Treeview", background=c["surface"], fieldbackground=c["surface"],
                    foreground=c["ink"], rowheight=26, bordercolor=c["border"])
    style.map("Treeview", background=[("selected", c["select"])],
              foreground=[("selected", c["ink"])])
    style.configure("Treeview.Heading", font=FONT_BOLD, background=c["button"], foreground=c["ink"])
    style.map("Treeview.Heading", background=[("active", c["button_active"])])
    style.configure("TScrollbar", background=c["button"], troughcolor=c["bg"], arrowcolor=c["ink"],
                    bordercolor=c["border"])
    style.map("TScrollbar", background=[("active", c["button_active"])])
    style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
    style.configure("TLabelframe.Label", font=FONT_BOLD, background=c["bg"], foreground=c["ink"])
    style.configure("Muted.TLabel", foreground=c["muted"], font=FONT_SMALL)
    style.configure("Warn.TLabel", foreground=c["warn"], font=FONT_BOLD)
    style.configure("Value.TLabel", font=FONT_BIG)
    style.configure("Title.TLabel", font=("Segoe UI", 13, "bold"))


def _descendants(widget: tk.Misc):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def apply_theme(root: tk.Tk, dark: bool) -> None:
    """Switch every window element to the chosen theme, including ones already on screen."""
    global _palette
    _palette = c = DARK if dark else LIGHT
    # Covers plain Tk parts: dropdown lists and pop-up prompts.
    root.tk_setPalette(background=c["bg"], foreground=c["ink"], activeBackground=c["button_active"],
                       activeForeground=c["ink"], selectBackground=c["select"],
                       selectForeground=c["ink"], insertBackground=c["ink"],
                       troughColor=c["panel"], disabledForeground=c["disabled"])
    _style(root, c)
    for widget in _descendants(root):
        if isinstance(widget, tk.Text):
            widget.configure(background=c["panel"], foreground=c["ink"],
                             insertbackground=c["ink"])
        elif hasattr(widget, "retheme"):
            widget.retheme()


def text_box(parent, **options) -> tk.Text:
    """A read-only text area that follows the theme."""
    c = colors()
    return tk.Text(parent, wrap="word", state="disabled", relief="flat", background=c["panel"],
                   foreground=c["ink"], insertbackground=c["ink"], font=FONT_SMALL, **options)


class Table(ttk.Frame):
    """A Treeview with a scrollbar that keeps the selection across refreshes."""

    def __init__(self, parent, columns: list[tuple[str, int]], height: int = 10):
        super().__init__(parent)
        names = [name for name, _ in columns]
        self.tree = ttk.Treeview(self, columns=names, show="headings", height=height,
                                 selectmode="browse")
        for name, width in columns:
            self.tree.heading(name, text=name)
            self.tree.column(name, width=width, minwidth=40, stretch=True, anchor="w")
        bar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def set_rows(self, rows: list[tuple[str, tuple]]) -> None:
        """rows: (stable id, values)."""
        selected = self.selected()
        self.tree.delete(*self.tree.get_children())
        for iid, values in rows:
            self.tree.insert("", "end", iid=iid, values=values)
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)

    def selected(self) -> str | None:
        chosen = self.tree.selection()
        return chosen[0] if chosen else None

    def on_select(self, callback) -> None:
        self.tree.bind("<<TreeviewSelect>>", lambda _e: callback())


class Tile(ttk.Frame):
    """A label with a large value beneath it."""

    def __init__(self, parent, label: str):
        super().__init__(parent, padding=(0, 0, 18, 6))
        ttk.Label(self, text=label, style="Muted.TLabel").pack(anchor="w")
        self.value = ttk.Label(self, text="-", style="Value.TLabel")
        self.value.pack(anchor="w")

    def set(self, text: str) -> None:
        self.value.configure(text=text)


def show_error(parent, message: str) -> None:
    messagebox.showerror("Paper Trader", message, parent=parent)


def show_info(parent, message: str) -> None:
    messagebox.showinfo("Paper Trader", message, parent=parent)


def form_row(parent, row: int, label: str, widget, hint: str = "") -> None:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
    widget.grid(row=row, column=1, sticky="we", pady=3)
    if hint:
        ttk.Label(parent, text=hint, style="Muted.TLabel").grid(row=row, column=2, sticky="w",
                                                                padx=(8, 0))
