# -*- coding: utf-8 -*-
"""
Ore UI 风格控件（Minecraft 基岩版 / Bedrock Editor 配色与描边）
=============================================================
设计参考：
  · 页面底色 #313233，卡片 #48494a，输入框 #2b2c2d
  · 主按钮绿 #3c8527（悬停 #52a535 / 按下 #2a641c），次按钮浅灰 #d0d1d4
  · 全部直角、2px 硬描边、无圆角；选中态用白色描边 / 白色下划线
  · 滑块：分段式轨道 + 绿色已填充 + 浅灰方形滑块

这里的控件都用 Canvas 手绘，才能做出参考图里那种“方块像素”质感。
"""

import tkinter as tk
import tkinter.font as tkfont

ORE = {
    "bg": "#313233",
    "panel": "#48494a",
    "panel_dark": "#3c3d3e",
    "field": "#2b2c2d",
    "ink": "#ffffff",
    "muted": "#a2a3a6",
    "border": "#1e1f20",
    "border_soft": "#6e6f72",
    "card_border": "#c8cacc",
    "header": "#c8cacc",
    "header_ink": "#1e1f20",
    "border_light": "#d0d1d4",
    "green": "#3c8527",
    "green_hi": "#52a535",
    "green_lo": "#2a641c",
    "gray": "#d0d1d4",
    "gray_hi": "#f4f6f9",
    "gray_lo": "#b1b2b5",
    "danger": "#ca3636",
    "danger_hi": "#ff8080",
}

FONT = "Microsoft YaHei UI"


def shade(color: str, factor: float) -> str:
    """按比例调亮/调暗颜色。"""
    color = color.lstrip("#")
    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    r = max(0, min(255, int(r * factor)))
    g = max(0, min(255, int(g * factor)))
    b = max(0, min(255, int(b * factor)))
    return f"#{r:02x}{g:02x}{b:02x}"


def parent_bg(widget, fallback=ORE["bg"]):
    """取父控件的底色，让控件和卡片自然融合。"""
    try:
        return widget.cget("background")
    except Exception:
        try:
            return widget.cget("bg")
        except Exception:
            return fallback


# --------------------------------------------------------------------------- #
# 按钮
# --------------------------------------------------------------------------- #
class OreButton(tk.Canvas):
    """Ore UI 风格按钮：primary（绿）/ secondary（浅灰）/ ghost（深色描边）/ danger。"""

    PALETTE = {
        "primary": ("green", "green_hi", "green_lo", "#ffffff"),
        "secondary": ("gray", "gray_hi", "gray_lo", "#1b1c1d"),
        "ghost": ("field", "panel", "border", "#ffffff"),
        "danger": ("danger", "danger_hi", "danger", "#ffffff"),
    }

    def __init__(self, master, text="", command=None, variant="secondary",
                 size="normal", width=None, font_size=10, bg=None, pad_x=14, **kw):
        self.var = variant
        self.size = size
        self.font_size = font_size
        self.command = command
        self.text = text
        self.pad_x = pad_x
        self.bg = bg or parent_bg(master)
        self.height = {"normal": 34, "small": 26, "big": 40}[size]
        self.state = "normal"
        self.hover = False
        self.pressed = False
        self.focused = False

        f = tkfont.Font(family=FONT, size=font_size,
                        weight="bold" if variant == "primary" else "normal")
        need = f.measure(text) + pad_x * 2 + 6
        super().__init__(master, height=self.height, width=width or need, bg=self.bg,
                         highlightthickness=0, bd=0, takefocus=1, cursor="hand2", **kw)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.bind("<space>", lambda e: self._activate())
        self.bind("<Return>", lambda e: self._activate())
        self.bind("<Configure>", lambda e: self._draw())

    # ---- 对外接口 ----
    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self.text = kw.pop("text")
        if "state" in kw:
            self.state = kw.pop("state")
        if "command" in kw:
            self.command = kw.pop("command")
        if "variant" in kw:
            self.var = kw.pop("variant")
        if kw:
            super().configure(cnf, **kw)
        self._draw()

    config = configure

    def set_state(self, state):
        self.configure(state=state)

    def is_disabled(self):
        return self.state == "disabled"

    # ---- 事件 ----
    def _on_enter(self, _e):
        if not self.is_disabled():
            self.hover = True
            self.configure(cursor="hand2")
            self._draw()

    def _on_leave(self, _e):
        self.hover = False
        self.pressed = False
        self._draw()

    def _on_press(self, _e):
        if self.is_disabled():
            return
        self.focus_set()
        self.pressed = True
        self._draw()

    def _on_release(self, e):
        if self.is_disabled():
            return
        was = self.pressed
        self.pressed = False
        self._draw()
        if was and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self._activate()

    def _activate(self):
        if self.is_disabled():
            return
        if self.command:
            self.command()

    def _set_focus(self, flag):
        self.focused = flag
        self._draw()

    # ---- 绘制 ----
    def _colors(self):
        key, hi, lo, fg = self.PALETTE[self.var]
        base = ORE.get(key, key)
        if self.is_disabled():
            fill = ORE["gray_lo"] if self.var != "ghost" else ORE["field"]
            return fill, shade(fill, 0.72), shade(fill, 1.06)
        if self.pressed:
            fill = ORE.get(lo, lo) if lo in ORE else shade(base, 0.75)
        elif self.hover:
            fill = ORE.get(hi, hi) if hi in ORE else shade(base, 1.15)
        else:
            fill = base
        return fill, fg, base

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        fill, fg, _ = self._colors()
        # 外层深色描边
        self.create_rectangle(0, 0, w - 1, h - 1, fill=fill, outline=ORE["border"],
                              width=2)
        # 上/左高光，下/右阴影（像素风的 1px 立体感）
        self.create_line(2, 2, w - 3, 2, fill=shade(fill, 1.28))
        self.create_line(2, 2, 2, h - 3, fill=shade(fill, 1.12))
        self.create_line(2, h - 3, w - 3, h - 3, fill=shade(fill, 0.78))
        self.create_line(w - 3, 2, w - 3, h - 3, fill=shade(fill, 0.86))
        # 文字
        weight = "bold" if self.var == "primary" else "normal"
        self.create_text(w // 2, h // 2 + 1, text=self.text, fill=fg,
                         font=(FONT, self.font_size, weight))
        # 焦点白框
        if self.focused and not self.is_disabled():
            self.create_rectangle(3, 3, w - 4, h - 4, outline="#ffffff", width=2)


# --------------------------------------------------------------------------- #
# 分段式滑块
# --------------------------------------------------------------------------- #
class OreSlider(tk.Canvas):
    """Ore UI 滑块：分段轨道 + 绿色填充 + 浅灰方形滑块。"""

    def __init__(self, master, variable, from_=0.0, to=100.0, command=None,
                 height=34, bg=None, step=None, **kw):
        self.bg = bg or parent_bg(master)
        self.variable = variable
        self.from_ = float(from_)
        self.to = float(to)
        self.command = command
        self.step = step
        self.hover = False
        self.focused = False
        self.dragging = False
        super().__init__(master, height=height, bg=self.bg, highlightthickness=0,
                         bd=0, takefocus=1, **kw)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.bind("<Left>", lambda e: self._nudge(-1))
        self.bind("<Right>", lambda e: self._nudge(1))
        self.bind("<Configure>", lambda e: self._draw())
        self._trace = self.variable.trace_add("write", lambda *a: self._draw())

    # ---- 数值 ----
    def _norm(self):
        span = self.to - self.from_ or 1.0
        return max(0.0, min(1.0, (float(self.variable.get()) - self.from_) / span))

    def _set_from_x(self, x):
        w = self.winfo_width()
        x0, x1 = 10, w - 10
        t = (x - x0) / max(1, (x1 - x0))
        t = max(0.0, min(1.0, t))
        value = self.from_ + t * (self.to - self.from_)
        if self.step:
            value = round(value / self.step) * self.step
        self.variable.set(round(value, 2))
        if self.command:
            self.command(self.variable.get())

    def _nudge(self, direction):
        span = self.to - self.from_
        step = self.step or span / 50.0
        self.variable.set(max(self.from_, min(self.to, self.variable.get() + step * direction)))
        if self.command:
            self.command(self.variable.get())

    def _set_hover(self, flag):
        self.hover = flag
        self.configure(cursor="hand2" if flag else "")
        self._draw()

    def _set_focus(self, flag):
        self.focused = flag
        self._draw()

    def _on_press(self, e):
        self.focus_set()
        self.dragging = True
        self._set_from_x(e.x)

    def _on_drag(self, e):
        if self.dragging:
            self._set_from_x(e.x)

    def _on_release(self, _e):
        self.dragging = False

    # ---- 绘制 ----
    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or h < 10:
            return
        x0, x1 = 10, w - 10
        cy = h // 2
        th = 12
        top, bot = cy - th // 2, cy + th // 2
        thumb_w, thumb_h = 18, 24
        pos = x0 + self._norm() * (x1 - x0)

        # 轨道
        self.create_rectangle(x0, top, x1, bot, fill=shade(ORE["panel"], 1.22),
                              outline=ORE["border"], width=2)
        # 绿色已填充
        self.create_rectangle(x0 + 1, top + 1, max(x0 + 2, pos), bot - 1,
                              fill=ORE["green"], outline="")
        # 分段刻度
        seg = 26
        x = x0 + seg
        while x < x1 - 4:
            self.create_line(x, top + 1, x, bot - 1, fill=ORE["bg"])
            x += seg
        # 滑块
        lx, rx = pos - thumb_w / 2, pos + thumb_w / 2
        ty0, ty1 = cy - thumb_h / 2, cy + thumb_h / 2
        self.create_rectangle(lx, ty0, rx, ty1,
                              fill=ORE["gray_hi"] if (self.hover or self.dragging)
                              else ORE["gray"],
                              outline=ORE["border"], width=2)
        self.create_line(lx + 2, ty0 + 2, rx - 2, ty0 + 2, fill="#ffffff")
        self.create_line(lx + 2, ty0 + 2, lx + 2, ty1 - 2, fill="#ffffff")
        if self.focused:
            self.create_rectangle(lx - 3, ty0 - 3, rx + 3, ty1 + 3,
                                  outline="#ffffff", width=2)


# --------------------------------------------------------------------------- #
# 标签页（分段切换）
# --------------------------------------------------------------------------- #
class OreTabs(tk.Frame):
    """Ore UI 标签按钮组：选中项深色底 + 白色下划线。"""

    def __init__(self, master, options, variable, command=None, bg=None,
                 height=38, font_size=10, **kw):
        self.bg = bg or parent_bg(master)
        super().__init__(master, bg=self.bg, **kw)
        self.variable = variable
        self.command = command
        self.options = list(options.items()) if isinstance(options, dict) else list(options)
        self.height = height
        self.font_size = font_size
        self.tabs = {}

        for i, (key, label) in enumerate(self.options):
            c = tk.Canvas(self, height=height, width=40, bg=self.bg,
                          highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 4, 0))
            self.columnconfigure(i, weight=1)
            c.bind("<ButtonPress-1>", lambda e, k=key: self._select(k))
            c.bind("<Enter>", lambda e, cc=c: self._hover(cc, True))
            c.bind("<Leave>", lambda e, cc=c: self._hover(cc, False))
            c.bind("<Configure>", lambda e, cc=c: self._draw_tab(cc))
            self.tabs[key] = c
        self._hover_key = None
        self.variable.trace_add("write", lambda *a: self._draw_all())
        self.after(10, self._draw_all)

    def _select(self, key):
        self.variable.set(key)
        if self.command:
            self.command(key)

    def _hover(self, canvas, flag):
        self._hover_key = canvas
        self._draw_tab(canvas)

    def _draw_all(self):
        for c in self.tabs.values():
            self._draw_tab(c)

    def _draw_tab(self, canvas):
        canvas.delete("all")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 4 or h < 4:
            return
        key = next(k for k, c in self.tabs.items() if c is canvas)
        active = str(self.variable.get()) == str(key)
        label = dict(self.options)[key]
        if active:
            fill, fg = ORE["green"], "#ffffff"
        else:
            fill, fg = ORE["gray"], "#1b1c1d"
        if canvas is self._hover_key and not active:
            fill = ORE["gray_hi"]
        canvas.create_rectangle(0, 0, w - 1, h - 1, fill=fill, outline=ORE["border"],
                                width=2)
        canvas.create_line(2, 2, w - 3, 2, fill=shade(fill, 1.18))
        canvas.create_text(w // 2, h // 2 + 1, text=label, fill=fg,
                           font=(FONT, self.font_size, "bold" if active else "normal"))
        if active:
            canvas.create_rectangle(8, h - 7, w - 9, h - 3, fill="#ffffff", outline="")


# --------------------------------------------------------------------------- #
# 复选框
# --------------------------------------------------------------------------- #
class OreCheck(tk.Canvas):
    """Ore UI 风格复选框：18px 方块 + 文字，选中变绿并打勾。"""

    def __init__(self, master, text="", variable=None, command=None, bg=None,
                 font_size=10, **kw):
        import tkinter.font as _tkfont

        self.var = variable if variable is not None else tk.BooleanVar(value=False)
        self.text = text
        self.bg = bg or parent_bg(master)
        self.command = command
        self.font_size = font_size
        f = _tkfont.Font(family=FONT, size=font_size)
        w = f.measure(text) + 18 + 12
        super().__init__(master, width=w, height=26, bg=self.bg, highlightthickness=0,
                         bd=0, takefocus=1, cursor="hand2", **kw)
        self.bind("<Button-1>", self._toggle)
        self.bind("<space>", self._toggle)
        self.bind("<Configure>", lambda e: self._draw())
        self.var.trace_add("write", lambda *a: self._draw())

    def _toggle(self, _e=None):
        self.var.set(not bool(self.var.get()))
        if self.command:
            self.command()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        box = 18
        x, y = 1, (h - box) // 2
        checked = bool(self.var.get())
        fill = ORE["green"] if checked else ORE["field"]
        self.create_rectangle(x, y, x + box, y + box, fill=fill, outline=ORE["border"],
                              width=2)
        if checked:
            self.create_line(x + 4, y + 9, x + 7, y + 13, x + 14, y + 5,
                             fill="#ffffff", width=2)
        self.create_text(x + box + 8, h // 2, anchor="w", text=self.text,
                         fill=ORE["ink"], font=(FONT, self.font_size))


# --------------------------------------------------------------------------- #
# 进度条
# --------------------------------------------------------------------------- #
class OreProgress(tk.Canvas):
    def __init__(self, master, height=18, bg=None, **kw):
        self.bg = bg or parent_bg(master)
        super().__init__(master, height=height, bg=self.bg, highlightthickness=0, bd=0, **kw)
        self._value = 0.0
        self._max = 1.0
        self.bind("<Configure>", lambda e: self._draw())

    def set_max(self, value):
        self._max = float(value) or 1.0
        self._draw()

    def set(self, value):
        self._value = float(value)
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        self.create_rectangle(0, 0, w - 1, h - 1, fill=ORE["field"],
                              outline=ORE["border"], width=2)
        ratio = max(0.0, min(1.0, self._value / self._max))
        if ratio > 0:
            self.create_rectangle(2, 2, max(3, int((w - 4) * ratio) + 2), h - 3,
                                  fill=ORE["green"], outline="")
            # 分段
            x = 8
            while x < int((w - 4) * ratio):
                self.create_line(x, 3, x, h - 4, fill=shade(ORE["green"], 0.82))
                x += 14


# --------------------------------------------------------------------------- #
# 基础容器
# --------------------------------------------------------------------------- #
def OreCard(master, bg=None, pad=0, border=2, **kw):
    """带 2px 描边的卡片。"""
    return tk.Frame(master, bg=bg or ORE["panel"], padx=pad, pady=pad,
                    highlightthickness=border,
                    highlightbackground=ORE["card_border"],
                    highlightcolor=ORE["card_border"], bd=0, **kw)

def OreLabel(master, text="", kind="body", bg=None, **kw):
    """kind: title / section / body / muted / value / tiny"""
    colors = {
        "title": (ORE["ink"], 13, "bold"),
        "section": (ORE["ink"], 10, "normal"),
        "body": (ORE["ink"], 10, "normal"),
        "muted": (ORE["muted"], 9, "normal"),
        "value": (ORE["ink"], 10, "bold"),
        "tiny": (ORE["muted"], 8, "normal"),
    }
    fg, size, weight = colors[kind]
    opts = dict(bg=bg or parent_bg(master), fg=fg, font=(FONT, size, weight),
                anchor="w", justify="left")
    opts.update(kw)
    return tk.Label(master, text=text, **opts)


class OreEntry(tk.Entry):
    """深色输入框：聚焦时白色 2px 描边。"""

    def __init__(self, master, textvariable=None, width=10, bg=None, font_size=10, **kw):
        self.bg = bg or ORE["field"]
        super().__init__(master, textvariable=textvariable, width=width, bg=self.bg,
                         fg=ORE["ink"], insertbackground=ORE["ink"], relief="flat",
                         bd=0, highlightthickness=2,
                         highlightbackground=ORE["border"],
                         highlightcolor="#ffffff",
                         font=(FONT, font_size), **kw)

    def set_bg(self, color):
        self.bg = color
        self.configure(bg=color)


class OreScrollbar(tk.Canvas):
    """细长像素风滚动条（配合 listbox / treeview / canvas 使用）。"""

    def __init__(self, master, command=None, bg=None, width=10, **kw):
        self.bg = bg or parent_bg(master)
        kw.setdefault("height", 40)
        super().__init__(master, width=width, bg=self.bg, highlightthickness=0, bd=0,
                         **kw)
        self.command = command
        self.first, self.last = 0.0, 1.0
        self._drag_offset = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_offset", None))

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self._draw()

    def _thumb(self):
        h = self.winfo_height()
        y0 = int(self.first * h)
        y1 = max(y0 + 16, int(self.last * h))
        return y0, min(y1, h)

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 4:
            return
        self.create_rectangle(2, 0, w - 3, h - 1, fill=ORE["field"],
                              outline=ORE["border"])
        if self.last - self.first >= 0.999:
            return
        y0, y1 = self._thumb()
        self.create_rectangle(3, y0, w - 4, y1, fill=ORE["border_soft"],
                              outline=ORE["border"])
        self.create_line(4, y0 + 2, w - 5, y0 + 2, fill=shade(ORE["border_soft"], 1.25))

    def _on_press(self, e):
        y0, y1 = self._thumb()
        if y0 <= e.y <= y1:
            self._drag_offset = e.y - y0
        else:
            self._drag_offset = (y1 - y0) // 2
            self._scroll_to(e.y)

    def _on_drag(self, e):
        if self._drag_offset is not None:
            self._scroll_to(e.y)

    def _scroll_to(self, y):
        if not self.command:
            return
        h = max(1, self.winfo_height())
        span = max(0.0, self.last - self.first)
        top = max(0.0, min(1.0 - span, (y - (self._drag_offset or 0)) / h))
        self.command("moveto", top)
