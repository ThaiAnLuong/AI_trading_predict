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
    # Soft row tints: the text keeps its normal colour, so it stays readable.
    "gain_bg": "#e2f3e5", "loss_bg": "#fbe4e4", "held_bg": "#fbf3c8",
}
DARK = {
    "bg": "#1f1f1e", "surface": "#1a1a19", "field": "#2a2a28", "panel": "#262624",
    "ink": "#f0efec", "muted": "#c3c2b7", "disabled": "#7d7c76", "grid": "#3a3a37",
    "border": "#4a4a46", "select": "#2f5f9e", "button": "#33332f", "button_active": "#42423d",
    "warn": "#f0b35c", "my": "#3987e5", "ai": "#d95926",
    "gain_bg": "#1f3326", "loss_bg": "#3f2326", "held_bg": "#3b3720",
}
ROW_TAGS = {"gain": "gain_bg", "loss": "loss_bg", "held": "held_bg"}

# Tables cannot bold a single column, so reference prices use the bold digit characters
# (Mathematical Sans-Serif Bold). Only the screen text changes; stored values and CSV
# exports keep plain numbers.
_BOLD_DIGITS = str.maketrans({str(i): chr(0x1D7EC + i) for i in range(10)})


def bold_num(text: str) -> str:
    """Show the digits of a number in bold inside a table cell."""
    return text.translate(_BOLD_DIGITS)


def plain_num(text: str) -> str:
    """Undo bold_num."""
    return text.translate({0x1D7EC + i: str(i) for i in range(10)})


def sign_tag(value) -> str:
    """'gain' for above zero, 'loss' for below, nothing for zero or unknown."""
    if value is None or value == 0:
        return ""
    return "gain" if value > 0 else "loss"
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
    # Hidden tab strip plus grouped tab buttons (1.3.0).
    style.layout("Hidden.TNotebook.Tab", [])
    style.configure("Hidden.TNotebook", background=c["bg"], borderwidth=0)
    style.configure("Tab.Toolbutton", background=c["button"], foreground=c["ink"], padding=(12, 5),
                    relief="flat", borderwidth=1, bordercolor=c["border"], anchor="center")
    style.map("Tab.Toolbutton",
              background=[("selected", c["select"]), ("active", c["button_active"])],
              foreground=[("selected", c["ink"])], relief=[("selected", "flat")])
    style.configure("Help.TLabel", foreground=c["my"], font=("Segoe UI", 9, "bold"),
                    padding=(4, 0))
    style.configure("Group.TLabel", foreground=c["muted"], font=("Segoe UI", 8, "bold"))
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


def note_popup(anchor: tk.Misc, text: str, x: int, y: int, width: int = 340) -> tk.Toplevel:
    """A small borderless note at screen position (x, y), kept on screen."""
    c = colors()
    top = tk.Toplevel(anchor)
    top.wm_overrideredirect(True)
    top.attributes("-topmost", True)
    frame = tk.Frame(top, background=c["border"], padx=1, pady=1)
    frame.pack()
    tk.Label(frame, text=text, justify="left", wraplength=width, background=c["field"],
             foreground=c["ink"], font=FONT_SMALL, padx=10, pady=8).pack()
    top.update_idletasks()
    window = anchor.winfo_toplevel()
    right_edge = min(window.winfo_rootx() + window.winfo_width(), anchor.winfo_screenwidth())
    if x + top.winfo_reqwidth() > right_edge:  # no room on the right: open to the left
        x = x - top.winfo_reqwidth() - 28
    x = max(0, min(x, anchor.winfo_screenwidth() - top.winfo_reqwidth() - 8))
    y = max(0, min(y, anchor.winfo_screenheight() - top.winfo_reqheight() - 48))
    top.wm_geometry(f"+{x}+{y}")
    return top


class Table(ttk.Frame):
    """A Treeview with a scrollbar that keeps the selection across refreshes.

    Select a row, then rest the mouse on it to see all of it, long text wrapped. Move off
    the row (or click) to close it.
    """

    HOVER_MS = 500

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
        self.retheme()
        self.names = names
        self.peek: tk.Toplevel | None = None
        self.pending = None
        self.peek_row: str | None = None  # the row the note is open, waiting or closed for
        self.tree.bind("<Motion>", self.on_hover, add="+")
        self.tree.bind("<ButtonPress-1>", self.on_press, add="+")
        self.tree.bind("<ButtonRelease-1>", self.on_hover, add="+")
        self.tree.bind("<Leave>", self.close_peek, add="+")
        self.tree.bind("<KeyPress>", self.close_peek, add="+")
        self.tree.bind("<MouseWheel>", self.close_peek, add="+")

    # -- rest the mouse on the selected row for the full row -------------------------

    def on_hover(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row or row not in self.tree.selection():
            self.close_peek()
        elif row != self.peek_row:  # same row: already open, waiting, or closed by a click
            self.close_peek()
            self.peek_row = row
            self.pending = self.tree.after(self.HOVER_MS, lambda: self.open_peek(
                row, event.x_root, event.y_root))

    def on_press(self, event) -> None:
        # a click closes the note; clicking the open row keeps it shut until the mouse
        # leaves that row
        was_open = self.peek is not None and self.peek_row == self.tree.identify_row(event.y)
        row = self.peek_row
        self.close_peek()
        if was_open:
            self.peek_row = row

    def row_text(self, row: str) -> str:
        lines = []
        for name, value in zip(self.names, self.tree.item(row, "values")):
            text = str(value).strip()
            if text and text != "-":
                lines.append(f"{name}:  {text}")
        return "\n".join(lines)

    def open_peek(self, row: str, x: int, y: int) -> None:
        self.pending = None
        if self.tree.exists(row):
            self.peek = note_popup(self.tree, self.row_text(row), x + 14, y + 10, width=520)

    def close_peek(self, _event=None) -> None:
        self.peek_row = None
        if self.pending is not None:
            self.tree.after_cancel(self.pending)
            self.pending = None
        if self.peek is not None:
            self.peek.destroy()
            self.peek = None

    def retheme(self) -> None:
        c = colors()
        for tag, key in ROW_TAGS.items():
            self.tree.tag_configure(tag, background=c[key])

    def set_rows(self, rows: list[tuple]) -> None:
        """rows: (stable id, values) or (stable id, values, row tag such as 'gain')."""
        selected = self.selected()
        self.tree.delete(*self.tree.get_children())
        for iid, values, *tag in rows:
            tags = (tag[0],) if tag and tag[0] else ()
            self.tree.insert("", "end", iid=iid, values=values, tags=tags)
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


class HelpTip:
    """A small "?" that explains a setting. Hover for half a second (or click) to open the
    note; move the mouse away (or click again) to close it."""

    DELAY_MS = 500

    def __init__(self, parent, text: str):
        self.text = text
        self.icon = ttk.Label(parent, text="?", style="Help.TLabel", cursor="question_arrow")
        self.popup: tk.Toplevel | None = None
        self.pending = None
        self.icon.bind("<Enter>", self.schedule)
        self.icon.bind("<Leave>", self.hide)
        self.icon.bind("<Button-1>", self.toggle)

    def grid(self, **options) -> None:
        self.icon.grid(**options)

    def pack(self, **options) -> None:
        self.icon.pack(**options)

    def schedule(self, _event=None) -> None:
        self.cancel()
        self.pending = self.icon.after(self.DELAY_MS, self.show)

    def cancel(self) -> None:
        if self.pending is not None:
            self.icon.after_cancel(self.pending)
            self.pending = None

    def toggle(self, _event=None) -> None:
        if self.popup is not None:
            self.hide()
        else:
            self.cancel()
            self.show()

    def show(self) -> None:
        self.pending = None
        if self.popup is not None:
            return
        self.popup = note_popup(self.icon, self.text,
                                self.icon.winfo_rootx() + self.icon.winfo_width() + 6,
                                self.icon.winfo_rooty() - 4)

    def hide(self, _event=None) -> None:
        self.cancel()
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None


def form_row(parent, row: int, label: str, widget, hint: str = "", help: str = "") -> None:
    """Label, input, then an optional "?" note and a short grey hint."""
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
    widget.grid(row=row, column=1, sticky="we", pady=3)
    if help:
        HelpTip(parent, help).grid(row=row, column=2, sticky="w", padx=(6, 0))
    if hint:
        ttk.Label(parent, text=hint, style="Muted.TLabel").grid(
            row=row, column=3 if help else 2, sticky="w", padx=(8, 0))


def check_row(parent, row: int, text: str, variable, help: str = "") -> ttk.Checkbutton:
    """A checkbox across the form, with an optional "?" note after it."""
    line = ttk.Frame(parent)
    line.grid(row=row, column=0, columnspan=4, sticky="w")
    box = ttk.Checkbutton(line, text=text, variable=variable)
    box.pack(side="left")
    if help:
        HelpTip(line, help).pack(side="left", padx=(6, 0))
    return box
