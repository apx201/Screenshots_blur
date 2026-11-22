#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
截图 UID 批量打码工具（Ore UI 风格）
===================================
界面参考 Minecraft 基岩版 Ore UI / Bedrock 编辑器暗色风格：
#313233 页面底色、#48494a 卡片、绿色主按钮 #3c8527、浅灰次按钮 #d0d1d4、
直角 + 2px 硬描边、分段式滑块。

用法：
  · 双击「启动打码工具.vbs」打开图形界面
  · 命令行批量：python uid_blur.py --cli -i 图片目录 -o 输出目录 --strength 7
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk

import ore_ui
from ore_ui import (FONT, ORE, OreButton, OreCard, OreCheck, OreEntry, OreLabel,
                    OreProgress, OreScrollbar, OreSlider, OreTabs, shade)

APP_NAME = "Wiki人的图片小工具"
APP_VERSION = "1.3"
SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

# 参考分辨率（样例截图 3168x1440）中 UID 文字的外框：x 250..464, y 1379..1422
REF_W, REF_H = 3168.0, 1440.0
REF_BOX = (250.0, 1379.0, 464.0, 1422.0)
# 默认打码区域（相对坐标 0~1：左、上、右、下）
DEFAULT_RECT = (
    REF_BOX[0] / REF_W,
    REF_BOX[1] / REF_H,
    REF_BOX[2] / REF_W,
    REF_BOX[3] / REF_H,
)

MODE_LABELS = {"gaussian": "高斯模糊", "mosaic": "马赛克", "solid": "纯色遮盖"}

# 裁剪比例（宽 / 高）
CROP_RATIOS = {"16:9": 16 / 9, "4:3": 4 / 3, "1:1": 1.0, "9:16": 9 / 16, "3:4": 3 / 4}


def crop_box_for(w, h, ratio_key, scale=1.0, cw=1920, ch=1080, ox=0.5, oy=0.5):
    """算出裁剪框（图像像素）：比例模式按最大内接 × scale，自定义模式按 cw×ch。"""
    if ratio_key == "custom":
        bw = max(1, min(int(round(cw)), int(w)))
        bh = max(1, min(int(round(ch)), int(h)))
    else:
        r = CROP_RATIOS.get(ratio_key, 16 / 9)
        bw, bh = int(w), int(round(w / r))
        if bh > h:
            bh, bw = int(h), int(round(h * r))
        s = max(0.1, min(1.0, float(scale)))
        bw = max(8, int(round(bw * s)))
        bh = max(8, int(round(bh * s)))
    x = int(round(ox * w - bw / 2))
    y = int(round(oy * h - bh / 2))
    x = max(0, min(int(w) - bw, x))
    y = max(0, min(int(h) - bh, y))
    return x, y, bw, bh


def crop_max_for(w, h, ratio_key):
    """比例模式下、scale=1 时的最大裁剪尺寸。"""
    if ratio_key == "custom":
        return int(w), int(h)
    r = CROP_RATIOS.get(ratio_key, 16 / 9)
    bw, bh = int(w), int(round(w / r))
    if bh > h:
        bh, bw = int(h), int(round(h * r))
    return max(1, bw), max(1, bh)


def crop_image(im: Image.Image, crop: dict) -> Image.Image:
    """按裁剪参数裁一张图；自定义尺寸时不足会放大到目标尺寸。"""
    w, h = im.size
    x, y, bw, bh = crop_box_for(w, h, crop.get("ratio", "16:9"),
                                crop.get("scale", 1.0), crop.get("w", 1920),
                                crop.get("h", 1080), crop.get("ox", 0.5),
                                crop.get("oy", 0.5))
    out = im.crop((x, y, x + bw, y + bh))
    if crop.get("ratio") == "custom":
        tw, th = max(1, int(round(crop.get("w", 1920)))), max(1, int(round(crop.get("h", 1080))))
        if out.size != (tw, th):
            out = out.resize((tw, th), Image.LANCZOS)
    return out


# --------------------------------------------------------------------------- #
# 图像处理核心（界面与命令行共用）
# --------------------------------------------------------------------------- #
def to_srgb(im: Image.Image) -> Image.Image:
    """带 ICC 配置的图片（如 Display P3 截图）统一转到 sRGB。

    不转的话，输出文件没有色彩配置，会被当成 sRGB 显示，颜色就偏了。
    """
    icc = im.info.pop("icc_profile", None)
    if not icc:
        return im
    try:
        import io

        from PIL import ImageCms

        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        dst = ImageCms.createProfile("sRGB")
        if im.mode == "RGBA":
            rgb = ImageCms.profileToProfile(im.convert("RGB"), src, dst,
                                            outputMode="RGB")
            rgb.putalpha(im.getchannel("A"))
            return rgb
        return ImageCms.profileToProfile(im, src, dst, outputMode="RGB")
    except Exception:
        return im


def load_image(path: Path) -> Image.Image:
    """读取图片，按 EXIF 方向摆正，统一成 RGB / RGBA 并转到 sRGB。"""
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    if getattr(im, "is_animated", False):
        im.seek(0)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
    elif im.mode != "RGB":
        im = im.convert("RGB")
    return to_srgb(im)


def _rect_to_px(rect, w: int, h: int):
    """相对坐标 -> 像素坐标（裁剪到图像内，保证宽高 >= 1）。"""
    x0 = int(round(rect[0] * w))
    y0 = int(round(rect[1] * h))
    x1 = int(round(rect[2] * w))
    y1 = int(round(rect[3] * h))
    x0, x1 = max(0, min(w - 1, x0)), max(0, min(w, x1))
    y0, y1 = max(0, min(h - 1, y0)), max(0, min(h, y1))
    if x1 <= x0:
        x1 = min(w, x0 + 1)
    if y1 <= y0:
        y1 = min(h, y0 + 1)
    return x0, y0, x1, y1


def radius_for(width: int, strength: float) -> int:
    """强度（千分之一图片宽度）换算成实际模糊半径 / 马赛克块大小。"""
    return max(1, int(round(width * strength / 1000.0)))


def apply_mask(im: Image.Image, rects, strength: float = 7.0, mode: str = "gaussian",
               feather: int = 2, fill=(0, 0, 0), radius=None) -> Image.Image:
    """在 im 的若干矩形区域上打码，返回新图像（不修改原图）。"""
    w, h = im.size
    if radius is None:
        radius = radius_for(w, strength)
    radius = max(1, int(radius))
    out = im.copy()

    for rect in rects:
        x0, y0, x1, y1 = _rect_to_px(rect, w, h)
        if x1 - x0 < 1 or y1 - y0 < 1:
            continue

        # 多取一圈上下文再模糊，避免贴回原图时边缘出现暗边
        pad = min(max(6, radius * 2 + 2), max(w, h))
        cx0, cy0 = max(0, x0 - pad), max(0, y0 - pad)
        cx1, cy1 = min(w, x1 + pad), min(h, y1 + pad)
        crop = out.crop((cx0, cy0, cx1, cy1))

        if mode == "mosaic":
            block = max(2, radius)
            sw = max(1, crop.width // block)
            sh = max(1, crop.height // block)
            proc = crop.resize((sw, sh), Image.BOX).resize(crop.size, Image.NEAREST)
        elif mode == "solid":
            proc = Image.new(crop.mode, crop.size, fill)
        else:  # gaussian
            proc = crop.filter(ImageFilter.GaussianBlur(radius))

        mask = Image.new("L", crop.size, 0)
        ImageDraw.Draw(mask).rectangle(
            (x0 - cx0, y0 - cy0, x1 - cx0 - 1, y1 - cy0 - 1), fill=255)
        if feather > 0:
            mask = mask.filter(ImageFilter.GaussianBlur(feather))
        out.paste(proc, (cx0, cy0), mask)

    return out


def save_image(im: Image.Image, path: Path, quality: int = 95) -> None:
    """按扩展名保存；不写入 EXIF / 原图 ICC，避免元数据被带出去。"""
    im.info.pop("icc_profile", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.save(path, "JPEG", quality=quality, subsampling=0, optimize=True)
    elif ext == ".png":
        im.save(path, "PNG", optimize=True)
    elif ext == ".webp":
        im.save(path, "WEBP", quality=quality, method=4)
    elif ext == ".bmp":
        im.save(path, "BMP")
    else:
        im.save(path)


def process_one(src: Path, dst: Path, rects=None, strength=7, mode="gaussian",
                feather=2, fill=(18, 20, 26), quality=95, crop=None) -> Path:
    im = load_image(src)
    if crop:
        out = crop_image(im, crop)
    else:
        out = apply_mask(im, rects, strength=strength, mode=mode,
                         feather=feather, fill=fill)
    save_image(out, dst, quality=quality)
    return dst


def make_output_stem(stem: str, number: int, prefix: str = "", suffix: str = "_已打码",
                     numbering: bool = False, pad: bool = False, width: int = 2) -> str:
    """生成输出文件名（不含扩展名）。

    · 填了前缀就用前缀，否则沿用原文件名
    · 勾选编号：末尾加「空格 + 序号」，如 `我的世界 1`
    · 未勾选编号：末尾加后缀，如 `我的世界_已打码`
    """
    base = (prefix or "").strip() or stem
    if numbering:
        num = str(int(number)).zfill(width) if pad else str(int(number))
        return f"{base} {num}"
    return f"{base}{suffix or ''}"


def build_jobs(items, outdir: Path, suffix: str, prefix: str = "", numbering: bool = False,
               start: int = 1, pad: bool = False, lossless: bool = False):
    """items: [(源文件, 相对路径), ...] -> [(源, 目标)]，自动避免同名覆盖。"""
    suffix = suffix if suffix is not None else "_已打码"
    start = max(0, int(start))
    total = len(items)
    width = max(2, len(str(start + max(0, total - 1)))) if pad else 2
    jobs, used = [], set()
    for i, (src, rel) in enumerate(items):
        rel = Path(rel)
        stem = make_output_stem(rel.stem, start + i, prefix, suffix, numbering, pad, width)
        ext = ".png" if lossless else rel.suffix
        dst = outdir / rel.parent / (stem + ext)
        try:  # 后缀/前缀被清空且输出目录=原目录时，避免覆盖原图
            if dst.resolve() == Path(src).resolve():
                dst = outdir / rel.parent / (stem + "_已打码" + ext)
        except OSError:
            pass
        n = 2
        while dst in used:
            dst = outdir / rel.parent / f"{stem} ({n}){ext}"
            n += 1
        used.add(dst)
        jobs.append((Path(src), dst))
    return jobs


def collect_inputs(inputs, recursive=True, suffix=""):
    """从文件/文件夹列表收集图片。返回 [(路径, 相对路径)] 与跳过的数量。"""
    items, skipped = [], 0
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in sorted(it):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXT:
                    if suffix and f.stem.endswith(suffix):
                        skipped += 1
                        continue
                    items.append((f, f.relative_to(p)))
        elif p.is_file():
            if p.suffix.lower() in SUPPORTED_EXT:
                items.append((p, Path(p.name)))
            else:
                skipped += 1
        else:
            skipped += 1
    return items, skipped


def parse_color(text, fallback=(18, 20, 26)):
    """把 #rgb / #rrggbb / rgb(r,g,b) / r,g,b 解析成 (r,g,b)。"""
    import re

    t = (text or "").strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{3})", t)
    if m:
        h = m.group(1)
        return tuple(int(c * 2, 16) for c in h)
    m = re.fullmatch(r"#([0-9a-f]{6})", t)
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,[^)]*)?\)", t)
    if m:
        return tuple(max(0, min(255, int(v))) for v in m.groups()[:3])
    m = re.fullmatch(r"(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})", t)
    if m:
        return tuple(max(0, min(255, int(v))) for v in m.groups())
    return fallback


def color_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb[:3])


def parse_rect(text: str):
    parts = [float(v) for v in text.replace(";", ",").split(",") if v.strip() != ""]
    if len(parts) != 4:
        raise ValueError("区域格式应为 x0,y0,x1,y1（相对比例，0~1）")
    x0, y0, x1, y1 = parts
    if x1 <= x0 or y1 <= y0:
        raise ValueError("区域右下角必须大于左上角")
    return (max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1))


# --------------------------------------------------------------------------- #
# 可滚动的右侧面板
# --------------------------------------------------------------------------- #
class ScrollFrame:
    def __init__(self, parent):
        import tkinter as tk

        self.outer = parent
        self.canvas = tk.Canvas(parent, bg=ORE["bg"], highlightthickness=0, bd=0,
                                width=440)
        self.vbar = OreScrollbar(parent, command=self.canvas.yview, bg=ORE["bg"],
                                 width=10)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")

        self.inner = tk.Frame(self.canvas, bg=ORE["bg"])
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>",
                         lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_inner(self, _=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self.win, width=event.width)
        self.canvas.configure(scrollregion=(0, 0, event.width,
                                            self.inner.winfo_reqheight()))

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #
class BlurApp:
    def __init__(self, root, initial_inputs=None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.ready = False
        self.items = []          # [(Path, Path rel)]
        self.rects = [list(DEFAULT_RECT)]
        self.sel = 0
        self.src_thumb = None
        self.src_full = None
        self.preview = None
        self.photo = None
        self.zoom_photo = None
        self.view = (0, 0, 1, 1)
        self.current_path = None
        self.drag = None
        self.show_before = False
        self.stop_event = threading.Event()
        self.worker = None
        self.q = queue.Queue()
        self.stats = {"ok": 0, "err": 0, "skip": 0}
        self.full_size = (0, 0)
        self._preview_job = None
        self._render_job = None
        self._thumb_scale = 1600
        self.recursive = True
        self._batch_total = 1

        self.strength = tk.DoubleVar(value=7.0)
        self.feather = tk.DoubleVar(value=2.0)
        self.mode = tk.StringVar(value="gaussian")
        self.quality = tk.IntVar(value=95)
        self.lossless = tk.BooleanVar(value=True)
        self.suffix = tk.StringVar(value="_已打码")
        self.prefix = tk.StringVar(value="")
        self.numbering = tk.BooleanVar(value=False)
        self.start_num = tk.StringVar(value="1")
        self.pad = tk.BooleanVar(value=False)
        self.outdir = tk.StringVar(value="")
        self.fill_color = (18, 20, 26)
        self.fill_text = tk.StringVar(value="#12141a")

        # 模式与裁剪
        self.work_mode = tk.StringVar(value="blur")
        self.crop_ratio = tk.StringVar(value="16:9")
        self.crop_scale = 1.0
        self.crop_offset = [0.5, 0.5]
        self.crop_w = tk.StringVar(value="1920")
        self.crop_h = tk.StringVar(value="1080")
        self.crop_drag = None

        self._build_style()
        self._build_ui()
        self._draw_swatch()
        self._sync_color_row()
        self._sync_quality_row()
        self._bind_shortcuts()
        self.ready = True
        self._refresh_list()
        self._render()

        if initial_inputs:
            self._add_paths(initial_inputs)

    # ---------------- 样式 / 布局 ----------------
    def _build_style(self):
        from tkinter import font as tkfont

        families = set(tkfont.families(self.root))
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "SimHei"):
            if name in families:
                global FONT
                FONT = name
                ore_ui.FONT = name
                break

        st = self.ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("Ore.Treeview", background=ORE["field"],
                     fieldbackground=ORE["field"], foreground=ORE["ink"],
                     bordercolor=ORE["border"], rowheight=26, borderwidth=0,
                     font=(FONT, 9))
        st.configure("Ore.Treeview.Heading", background=ORE["panel_dark"],
                     foreground=ORE["muted"], relief="flat",
                     font=(FONT, 9, "bold"), bordercolor=ORE["border"])
        st.map("Ore.Treeview", background=[("selected", ORE["green"])],
               foreground=[("selected", "#ffffff")])
        st.configure("Ore.Vertical.TScrollbar", background=ORE["border_soft"],
                     troughcolor=ORE["field"], bordercolor=ORE["border"],
                     darkcolor=ORE["panel"], lightcolor=ORE["panel"],
                     arrowcolor=ORE["muted"], arrowsize=12, gripcount=0,
                     relief="flat", borderwidth=0)
        st.map("Ore.Vertical.TScrollbar",
               background=[("active", ORE["muted"]), ("pressed", ORE["muted"])])

    def _build_ui(self):
        tk, ttk = self.tk, self.ttk
        root = self.root
        root.configure(bg=ORE["bg"])
        root.title(f"{APP_NAME} v{APP_VERSION}")
        root.geometry(f"{min(1340, root.winfo_screenwidth() - 60)}x"
                      f"{min(1020, root.winfo_screenheight() - 60)}")
        root.minsize(1120, 720)

        # ---- 顶部标题栏 ----
        head = tk.Frame(root, bg=ORE["header"], height=54)
        head.pack(fill="x")
        head.pack_propagate(False)

        logo = tk.Canvas(head, width=30, height=30, bg=ORE["header"],
                         highlightthickness=0, bd=0)
        logo.place(x=16, rely=0.5, anchor="w")
        for i in range(4):
            for j in range(4):
                col = ORE["green"] if (i + j) % 2 == 0 else ORE["green_lo"]
                if i + j > 4:
                    col = ORE["border_soft"] if (i + j) % 2 == 0 else ORE["panel"]
                logo.create_rectangle(3 + i * 6, 3 + j * 6, 8 + i * 6, 8 + j * 6,
                                      fill=col, outline="")
        self._set_icon()

        tk.Label(head, text=APP_NAME, bg=ORE["header"], fg=ORE["header_ink"],
                 font=(FONT, 14, "bold")).place(relx=0.5, rely=0.5, anchor="center")
        self.count_label = tk.Label(head, text="", bg=ORE["header"],
                                    fg="#4a4b4d", font=(FONT, 10))
        self.count_label.pack(side="right", padx=18)
        tk.Frame(root, bg=ORE["border"], height=2).pack(fill="x")

        # ---- 底部操作栏 ----
        bottom = tk.Frame(root, bg=ORE["panel_dark"])
        bottom.pack(fill="x", side="bottom")
        tk.Frame(bottom, bg=ORE["border"], height=2).pack(fill="x")

        self.status = tk.Label(bottom, text="就绪 · 请先添加图片", bg=ORE["panel_dark"],
                               fg=ORE["muted"], anchor="w", font=(FONT, 9),
                               padx=16, pady=5)
        self.status.pack(fill="x")

        bar = tk.Frame(bottom, bg=ORE["panel_dark"])
        bar.pack(fill="x", padx=16, pady=(0, 12))
        self.progress = OreProgress(bar, width=520, bg=ORE["panel_dark"])
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 16))
        self.btn_start = OreButton(bar, "开始批量打码", command=self.start_batch,
                                   variant="primary", size="big", width=186, font_size=11,
                                   bg=ORE["panel_dark"])
        self.btn_start.pack(side="right")
        self.btn_stop = OreButton(bar, "停止", command=self.stop_batch, variant="secondary",
                                  width=78, bg=ORE["panel_dark"])
        self.btn_stop.pack(side="right", padx=(0, 8))
        self.btn_open = OreButton(bar, "打开输出目录", command=self.open_outdir,
                                  variant="secondary", width=118, bg=ORE["panel_dark"])
        self.btn_open.pack(side="right", padx=(0, 8))

        # ---- 主体 ----
        body = tk.Frame(root, bg=ORE["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=12)

        # 左：预览
        left = tk.Frame(body, bg=ORE["bg"])
        left.pack(side="left", fill="both", expand=True)

        pv_card = OreCard(left, pad=0)
        pv_card.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(pv_card, bg=ORE["field"], highlightthickness=0, bd=0,
                                width=240, height=200, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=2, pady=2)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<ButtonPress-3>", lambda e: self._toggle_before(True))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self._toggle_before(False))

        self.zoom_card = zoom_card = OreCard(left, pad=10)
        zoom_card.pack(fill="x", pady=(10, 0))
        zhead = tk.Frame(zoom_card, bg=ORE["panel"])
        zhead.pack(fill="x")
        OreLabel(zhead, "区域放大预览", kind="section",
                 bg=ORE["panel"]).pack(side="left")
        self.zoom_label = OreLabel(zhead, "", kind="muted", bg=ORE["panel"])
        self.zoom_label.pack(side="right")
        self.zoom = tk.Canvas(zoom_card, bg=ORE["field"], width=240, height=128,
                              highlightthickness=0, bd=0)
        self.zoom.pack(fill="x", pady=(8, 0))
        self.zoom.bind("<Configure>", lambda e: self._render_zoom())

        self.log_card = log_card = OreCard(left, pad=10)
        log_card.pack(fill="x", pady=(10, 0))
        OreLabel(log_card, "日志", kind="section", bg=ORE["panel"]).pack(anchor="w")
        self.log = tk.Text(log_card, height=3, width=20, bg=ORE["field"], fg=ORE["muted"], bd=0,
                           wrap="word", highlightthickness=2,
                           highlightbackground=ORE["border"], font=(FONT, 9),
                           state="disabled", padx=6, pady=4)
        self.log.pack(fill="x", pady=(6, 0))
        self.log.tag_configure("ok", foreground="#7fd45f")
        self.log.tag_configure("err", foreground="#ff8080")
        self.log.tag_configure("info", foreground=ORE["muted"])

        # 右：设置面板
        right = tk.Frame(body, bg=ORE["bg"], width=456)
        right.pack(side="right", fill="y", padx=(12, 0))
        right.pack_propagate(False)
        self.scroll = ScrollFrame(right)
        panel = self.scroll.inner
        self.card = OreCard(panel, pad=8)
        self.card.pack(fill="x")

        self.work_tabs = OreTabs(self.card, {"blur": "打码", "crop": "裁剪"},
                                 self.work_mode,
                                 command=lambda k: self._on_mode_change())
        self.work_tabs.pack(fill="x")
        self._separator(self.card)

        self._build_images_section(self.card)
        self._separator(self.card)

        self.work_holder = tk.Frame(self.card, bg=ORE["panel"])
        self.work_holder.pack(fill="x")
        self.blur_box = tk.Frame(self.work_holder, bg=ORE["panel"])
        self.blur_box.pack(fill="x")
        self._build_region_section(self.blur_box)
        self._separator(self.blur_box)
        self._build_effect_section(self.blur_box)

        self.crop_box = tk.Frame(self.work_holder, bg=ORE["panel"])
        self._build_crop_section(self.crop_box)

        self._separator(self.card)
        self._build_output_section(self.card)
        self.canvas.focus_set()

    def _set_icon(self):
        tk = self.tk
        img = tk.PhotoImage(width=16, height=16)
        img.put(ORE["border"], to=(0, 0, 16, 16))
        img.put(ORE["green_lo"], to=(1, 1, 15, 15))
        for i in range(4):
            for j in range(4):
                col = ORE["green"] if (i + j) % 2 == 0 else ORE["green_lo"]
                img.put(col, to=(1 + i * 3, 1 + j * 3, 4 + i * 3, 4 + j * 3))
        self.root.iconphoto(True, img)
        self._icon = img

    def _separator(self, parent):
        self.tk.Frame(parent, bg=ORE["bg"], height=1).pack(fill="x", pady=5)

    def _section(self, parent, title, desc=None):
        box = self.tk.Frame(parent, bg=ORE["panel"])
        box.pack(fill="x")
        OreLabel(box, title, kind="section").pack(anchor="w")
        if desc:
            OreLabel(box, desc, kind="muted", wraplength=396,
                     justify="left").pack(anchor="w", pady=(3, 0))
        return box

    # ---- 图片列表 ----
    def _build_images_section(self, parent):
        tk, ttk = self.tk, self.ttk
        box = self._section(parent, "图片列表")

        wrap = tk.Frame(box, bg=ORE["field"], highlightthickness=2,
                        highlightbackground=ORE["border"])
        wrap.pack(fill="x", pady=(8, 8))
        self.tree = ttk.Treeview(wrap, columns=("name",), show="headings", height=3,
                                 selectmode="extended", style="Ore.Treeview")
        self.tree.heading("name", text="文件名", anchor="w")
        self.tree.column("name", anchor="w", width=360)
        sb = OreScrollbar(wrap, command=self.tree.yview, bg=ORE["field"])
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())

        btns = tk.Frame(box, bg=ORE["panel"])
        btns.pack(fill="x")
        OreButton(btns, "添加图片", command=self.add_files, variant="secondary",
                  size="small", width=92).pack(side="left")
        OreButton(btns, "添加文件夹", command=self.add_folder, variant="secondary",
                  size="small", width=104).pack(side="left", padx=6)
        OreButton(btns, "移除", command=self.remove_selected, variant="ghost",
                  size="small", width=58).pack(side="left")
        OreButton(btns, "清空", command=self.clear_files, variant="ghost",
                  size="small", width=58).pack(side="left", padx=(6, 0))

    # ---- 打码区域 ----
    def _build_region_section(self, parent):
        tk, ttk = self.tk, self.ttk
        box = self._section(parent, "打码区域",
                            "拖动预览图上的方框调整位置·拖四角缩放·右键对比原图")

        wrap = tk.Frame(box, bg=ORE["field"], highlightthickness=2,
                        highlightbackground=ORE["border"])
        wrap.pack(fill="x", pady=(8, 6))
        self.regions = tk.Listbox(wrap, height=2, bg=ORE["field"], fg=ORE["ink"],
                                  bd=0, selectbackground=ORE["green"],
                                  selectforeground="#ffffff", highlightthickness=0,
                                  activestyle="none", font=(FONT, 9))
        self.regions.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        self.regions.bind("<<ListboxSelect>>", self._on_region_select)
        sb = OreScrollbar(wrap, command=self.regions.yview, bg=ORE["field"])
        self.regions.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        btns = tk.Frame(box, bg=ORE["panel"])
        btns.pack(fill="x", pady=(0, 8))
        OreButton(btns, "+ 新区域", command=self.add_region, variant="ghost",
                  size="small", width=86).pack(side="left")
        OreButton(btns, "删除", command=self.del_region, variant="ghost",
                  size="small", width=58).pack(side="left", padx=6)
        OreButton(btns, "恢复默认", command=self.reset_region, variant="secondary",
                  size="small", width=86).pack(side="right")

        grid = tk.Frame(box, bg=ORE["panel"])
        grid.pack(fill="x")
        self.entry_vars = {}
        for i, (key, label) in enumerate((("x", "X %"), ("y", "Y %"),
                                          ("w", "宽 %"), ("h", "高 %"))):
            cell = tk.Frame(grid, bg=ORE["panel"])
            cell.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0), pady=2)
            OreLabel(cell, label, kind="tiny").pack(side="left", padx=(0, 4))
            var = tk.StringVar()
            self.entry_vars[key] = var
            e = OreEntry(cell, textvariable=var, width=5)
            e.pack(side="left", fill="x", expand=True)
            e.bind("<Return>", self._apply_entries)
            e.bind("<FocusOut>", self._apply_entries)
            grid.columnconfigure(i, weight=1)

    # ---- 打码效果 ----
    def _build_effect_section(self, parent):
        tk, ttk = self.tk, self.ttk
        box = self._section(parent, "打码效果")

        self.mode_tabs = OreTabs(box, MODE_LABELS, self.mode, height=42,
                                 command=lambda k: self._on_effect_change())
        self.mode_tabs.pack(fill="x", pady=(8, 10))

        # 纯色模式的颜色选择
        self.color_row = tk.Frame(box, bg=ORE["panel"])
        OreLabel(self.color_row, "颜色", kind="body").pack(side="left")
        self.swatch = tk.Canvas(self.color_row, width=36, height=28, bg=ORE["panel"],
                                highlightthickness=2, highlightbackground=ORE["border"],
                                bd=0, cursor="hand2")
        self.swatch.pack(side="left", padx=(8, 8))
        self.swatch.bind("<Button-1>", lambda e: self.pick_color())
        OreEntry(self.color_row, textvariable=self.fill_text, width=15).pack(side="left")
        OreButton(self.color_row, "取色…", command=self.pick_color, variant="ghost",
                  size="small", width=66).pack(side="left", padx=(8, 0))
        self.fill_text_entry = self.color_row.winfo_children()[-2]
        self.fill_text_entry.bind("<Return>", self._apply_fill_text)
        self.fill_text_entry.bind("<FocusOut>", self._apply_fill_text)

        srow = tk.Frame(box, bg=ORE["panel"])
        srow.pack(fill="x")
        self.strength_row = srow
        OreLabel(srow, "模糊强度", kind="body").pack(side="left")
        self.strength_value = OreLabel(srow, "7.0", kind="value")
        self.strength_value.pack(side="right")
        self.strength_slider = OreSlider(box, self.strength, 1, 40, height=30,
                                         command=lambda v: self._on_strength())
        self.strength_slider.pack(fill="x", pady=(2, 0))

        frow = tk.Frame(box, bg=ORE["panel"])
        frow.pack(fill="x", pady=(6, 0))
        self.feather_row = frow
        OreLabel(frow, "边缘羽化", kind="body").pack(side="left")
        self.feather_value = OreLabel(frow, "2 px", kind="value")
        self.feather_value.pack(side="right")
        OreSlider(box, self.feather, 0, 20, height=30, step=1,
                  command=lambda v: self._on_feather()).pack(fill="x", pady=(2, 0))

        self.strength_hint = OreLabel(box, "", kind="muted", wraplength=396,
                                      justify="left")
        self.strength_hint.pack(anchor="w", pady=(8, 0))

        self.strength.trace_add("write", lambda *a: self._on_effect_change())
        self.feather.trace_add("write", lambda *a: self._on_effect_change())

    # ---- 输出 ----
    def _build_output_section(self, parent):
        tk, ttk = self.tk, self.ttk
        box = self._section(parent, "输出")

        drow = tk.Frame(box, bg=ORE["panel"])
        drow.pack(fill="x", pady=(8, 6))
        OreEntry(drow, textvariable=self.outdir).pack(side="left", fill="x",
                                                      expand=True)
        OreButton(drow, "浏览", command=self.choose_outdir, variant="ghost",
                  size="small", width=62).pack(side="left", padx=(8, 0))

        prow = tk.Frame(box, bg=ORE["panel"])
        prow.pack(fill="x", pady=(0, 4))
        OreLabel(prow, "前缀", kind="muted").pack(side="left")
        OreEntry(prow, textvariable=self.prefix, width=12).pack(side="left", padx=(6, 10))
        self.name_example = OreLabel(prow, "", kind="muted")
        self.name_example.pack(side="right")

        nrow = tk.Frame(box, bg=ORE["panel"])
        nrow.pack(fill="x", pady=(0, 6))
        OreCheck(nrow, "顺序编号", variable=self.numbering,
                 command=self._on_name_change).pack(side="left")
        OreLabel(nrow, "起始", kind="muted").pack(side="left", padx=(14, 4))
        OreEntry(nrow, textvariable=self.start_num, width=5).pack(side="left")
        OreCheck(nrow, "补零", variable=self.pad,
                 command=self._on_name_change).pack(side="left", padx=(12, 0))

        orow = tk.Frame(box, bg=ORE["panel"])
        orow.pack(fill="x")
        OreLabel(orow, "后缀", kind="muted").pack(side="left")
        OreEntry(orow, textvariable=self.suffix, width=10).pack(side="left", padx=(6, 12))
        OreCheck(orow, "无损 PNG", variable=self.lossless,
                 command=self._on_lossless_change).pack(side="left")

        self.qrow = tk.Frame(box, bg=ORE["panel"])
        OreLabel(self.qrow, "画质", kind="muted").pack(side="left")
        qbox = tk.Frame(self.qrow, bg=ORE["panel"], width=150, height=30)
        qbox.pack(side="left", padx=(8, 8))
        qbox.pack_propagate(False)
        OreSlider(qbox, self.quality, 60, 100, height=30, step=1,
                  command=lambda v: self._update_hints()).pack(fill="both", expand=True)
        self.quality_label = OreLabel(self.qrow, "95", kind="value")
        self.quality_label.pack(side="left")
        self.quality.trace_add("write", lambda *a: self._update_hints())
        for v in (self.prefix, self.start_num, self.numbering, self.pad):
            v.trace_add("write", lambda *a: self._update_name_example())

    # ---------------- 模式 / 裁剪 ----------------
    def _on_mode_change(self):
        crop = self.work_mode.get() == "crop"
        if crop:
            self.blur_box.pack_forget()
            self.crop_box.pack(fill="x")
            self.zoom_card.pack_forget()
            self.btn_start.configure(text="开始批量裁剪")
            if self.suffix.get().strip() == "_已打码":
                self.suffix.set("_裁剪")
        else:
            self.crop_box.pack_forget()
            self.blur_box.pack(fill="x")
            if not self.zoom_card.winfo_manager():
                self.zoom_card.pack(fill="x", pady=(10, 0), before=self.log_card)
            self.btn_start.configure(text="开始批量打码")
            if self.suffix.get().strip() == "_裁剪":
                self.suffix.set("_已打码")
            if self.current_path is not None:
                self.status.configure(
                    text=f"{self.current_path.name}    {self.full_size[0]}×{self.full_size[1]}"
                         f"    区域 {len(self.rects)} 个")
        self._update_name_example()
        self._update_crop_info()
        self._schedule_preview()

    def _num(self, var, default):
        try:
            return float(str(var.get()).strip() or default)
        except (TypeError, ValueError):
            return float(default)

    def _crop_size_px(self):
        """当前裁剪输出尺寸（像素）。"""
        if self.src_full is None:
            return 0, 0
        w, h = self.src_full.size
        if self.crop_ratio.get() == "custom":
            return (max(1, int(round(self._num(self.crop_w, 1920)))),
                    max(1, int(round(self._num(self.crop_h, 1080)))))
        bw, bh = crop_max_for(w, h, self.crop_ratio.get())
        s = max(0.1, min(1.0, self.crop_scale))
        return max(8, int(round(bw * s))), max(8, int(round(bh * s)))

    def _crop_box(self, w, h):
        """按当前设置算出裁剪框（返回 x, y, w, h）。自定义尺寸从原图换算到预览坐标。"""
        fw = self.full_size[0] or w
        k = w / max(1, fw)
        return crop_box_for(w, h, self.crop_ratio.get(), self.crop_scale,
                            self._num(self.crop_w, 1920) * k,
                            self._num(self.crop_h, 1080) * k,
                            self.crop_offset[0], self.crop_offset[1])

    def _build_crop_section(self, parent):
        tk = self.tk
        box = self._section(parent, "裁剪设置")
        OreTabs(box, {"16:9": "16:9", "4:3": "4:3", "1:1": "1:1",
                      "9:16": "9:16", "3:4": "3:4", "custom": "自定义"},
                self.crop_ratio,
                command=lambda k: self._on_crop_ratio_change()).pack(fill="x", pady=(8, 0))

        self.crop_size_row = tk.Frame(box, bg=ORE["panel"])
        OreLabel(self.crop_size_row, "宽", kind="muted").pack(side="left")
        OreEntry(self.crop_size_row, textvariable=self.crop_w,
                 width=6).pack(side="left", padx=(4, 12))
        OreLabel(self.crop_size_row, "高", kind="muted").pack(side="left")
        OreEntry(self.crop_size_row, textvariable=self.crop_h,
                 width=6).pack(side="left", padx=(4, 12))
        OreLabel(self.crop_size_row, "px", kind="muted").pack(side="left")

        self.crop_hint = OreLabel(box, "", kind="muted", wraplength=396, justify="left")
        self.crop_hint.pack(anchor="w", pady=(8, 0))
        self.crop_hint2 = OreLabel(
            box, "拖动预览里的白框移动位置，拖四个角/边可缩放（所有图片共用同一位置）。",
            kind="muted", wraplength=396, justify="left")
        self.crop_hint2.pack(anchor="w", pady=(4, 0))

        self.crop_w.trace_add("write", lambda *a: self._on_crop_size_change())
        self.crop_h.trace_add("write", lambda *a: self._on_crop_size_change())
        self._sync_crop_size_row()

    def _sync_crop_size_row(self):
        if not hasattr(self, "crop_size_row"):
            return
        if self.crop_ratio.get() == "custom":
            if not self.crop_size_row.winfo_manager():
                self.crop_size_row.pack(fill="x", pady=(8, 0), before=self.crop_hint)
        else:
            self.crop_size_row.pack_forget()

    def _on_crop_ratio_change(self):
        self.crop_scale = 1.0
        self.crop_offset = [0.5, 0.5]
        self._sync_crop_size_row()
        self._update_crop_info()
        self._schedule_preview()

    def _on_crop_size_change(self):
        self._update_crop_info()
        self._schedule_preview()

    def _update_crop_info(self):
        if not hasattr(self, "crop_hint"):
            return
        bw, bh = self._crop_size_px()
        if self.src_full is None:
            self.crop_hint.configure(text="添加图片后可预览裁剪效果")
            return
        if self.crop_ratio.get() == "custom":
            self.crop_hint.configure(text=f"输出 {bw}×{bh} px · 原图不够大时会放大")
        else:
            self.crop_hint.configure(text=f"{self.crop_ratio.get()} · 输出 {bw}×{bh} px")
        if self.work_mode.get() == "crop" and self.current_path is not None:
            self.status.configure(
                text=f"{self.current_path.name}    {self.full_size[0]}×{self.full_size[1]}"
                     f"  →  {bw}×{bh}    {self.crop_ratio.get()}")

    # ---------------- 事件与快捷键 ----------------
    def _bind_shortcuts(self):
        self.root.bind("<Control-o>", lambda e: self.add_files())
        self.root.bind("<Control-O>", lambda e: self.add_files())
        self.root.bind("<Delete>", lambda e: self.del_region())
        self.root.bind("<Control-Return>", lambda e: self.start_batch())
        self.root.report_callback_exception = self._on_tk_error

    def _on_tk_error(self, exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        self._log("界面异常：" + str(val), "err")
        try:
            from tkinter import messagebox

            messagebox.showerror("出错了", f"{val}\n\n详细信息已写入界面日志。")
        except Exception:
            pass
        sys.stderr.write(text)

    # ---------------- 图片列表 ----------------
    def add_files(self):
        from tkinter import filedialog

        paths = filedialog.askopenfilenames(
            title="选择图片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff"),
                       ("全部文件", "*.*")],
        )
        if paths:
            self._add_paths(paths)

    def add_folder(self):
        from tkinter import filedialog

        folder = filedialog.askdirectory(title="选择图片文件夹（含子文件夹）")
        if folder:
            self._add_paths([folder])

    def _add_paths(self, paths):
        items, skipped = collect_inputs(paths, recursive=self.recursive,
                                        suffix=self.suffix.get().strip())
        existing = {str(p) for p, _ in self.items}
        added = 0
        for src, rel in items:
            if str(src) in existing:
                continue
            self.items.append((src, rel))
            existing.add(str(src))
            added += 1
        self._refresh_list()
        if skipped:
            self._log(f"跳过 {skipped} 个非图片或已打码文件", "info")
        if added:
            self._log(f"添加 {added} 张图片", "ok")
            if not self.outdir.get().strip():
                first = self.items[0][0]
                self.outdir.set(str(first.parent / "打码输出"))
            self._select_index(0)

    def _refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        for i, (src, rel) in enumerate(self.items):
            self.tree.insert("", "end", iid=str(i), values=(str(rel),))
        self.count_label.configure(text=f"共 {len(self.items)} 张")
        if not self.items:
            self.status.configure(text="就绪 · 请先添加图片")

    def _select_index(self, idx):
        if not self.items:
            return
        idx = max(0, min(idx, len(self.items) - 1))
        self.tree.selection_set(str(idx))
        self.tree.focus(str(idx))
        self.tree.see(str(idx))

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.items):
            self.load_preview(self.items[idx][0])

    def remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        for iid in sorted((int(s) for s in sel), reverse=True):
            if 0 <= iid < len(self.items):
                del self.items[iid]
        self._refresh_list()
        if self.items:
            self._select_index(0)
        else:
            self._reset_preview()

    def clear_files(self):
        self.items.clear()
        self._reset_preview()
        self.progress.set(0)

    def _reset_preview(self):
        self.src_thumb = None
        self.src_full = None
        self.preview = None
        self.current_path = None
        self._refresh_list()
        self._update_crop_info()
        self._render()

    # ---------------- 预览 ----------------
    def load_preview(self, path):
        try:
            im = load_image(path)
        except Exception as exc:
            from tkinter import messagebox

            messagebox.showerror("无法打开图片", f"{path}\n\n{exc}")
            return
        self.current_path = path
        self.full_size = im.size
        thumb = im.copy()
        thumb.thumbnail((self._thumb_scale, self._thumb_scale), Image.LANCZOS)
        self.src_thumb = thumb
        self.src_full = im
        self._update_hints()
        self._update_crop_info()
        self._rebuild_preview()
        self._sync_entries()
        self.status.configure(
            text=f"{path.name}    {im.size[0]}×{im.size[1]}    区域 {len(self.rects)} 个"
        )

    def _on_strength(self):
        self._update_hints()
        self._schedule_preview()

    def _on_feather(self):
        self._update_hints()
        self._schedule_preview()

    def _on_effect_change(self):
        self._update_hints()
        self._sync_color_row()
        self._schedule_preview()

    def _sync_color_row(self):
        """纯色模式：显示颜色行，隐藏强度行（纯色用不到强度）。"""
        if not hasattr(self, "color_row"):
            return
        solid = self.mode.get() == "solid"
        # 注意：pack(before=X) 要求 X 当前已 pack，所以统一以始终显示的 feather_row 为锚点
        if solid:
            if not self.color_row.winfo_manager():
                self.color_row.pack(fill="x", pady=(2, 6), before=self.feather_row)
            self.strength_row.pack_forget()
            self.strength_slider.pack_forget()
            self.strength_hint.pack_forget()
        else:
            self.color_row.pack_forget()
            if not self.strength_row.winfo_manager():
                self.strength_row.pack(fill="x", before=self.feather_row)
            if not self.strength_slider.winfo_manager():
                self.strength_slider.pack(fill="x", pady=(2, 0),
                                          before=self.feather_row)
            if not self.strength_hint.winfo_manager():
                self.strength_hint.pack(anchor="w", pady=(8, 0))

    def _set_fill(self, rgb, sync_text=True):
        self.fill_color = tuple(max(0, min(255, int(v))) for v in rgb[:3])
        if sync_text:
            self.fill_text.set(color_to_hex(self.fill_color))
        self._draw_swatch()
        self._schedule_preview()

    def _draw_swatch(self):
        if not hasattr(self, "swatch"):
            return
        self.swatch.delete("all")
        self.swatch.create_rectangle(0, 0, 36, 28, fill=color_to_hex(self.fill_color),
                                     outline="")

    def _apply_fill_text(self, _event=None):
        rgb = parse_color(self.fill_text.get(), fallback=None)
        if rgb:
            self._set_fill(rgb)
        else:
            self.fill_text.set(color_to_hex(self.fill_color))

    def pick_color(self):
        from tkinter import colorchooser

        rgb, hexv = colorchooser.askcolor(color=color_to_hex(self.fill_color),
                                          title="选择打码颜色")
        if hexv:
            self._set_fill(parse_color(hexv))

    def _update_hints(self):
        s = self.strength.get()
        self.strength_value.configure(text=f"{s:.1f}")
        self.feather_value.configure(text=f"{int(round(self.feather.get()))} px")
        if self.src_thumb is not None:
            w = self.full_size[0]
            self.strength_hint.configure(
                text=f"当前 {w}px 宽 → 半径/块约 {radius_for(w, s)}px"
            )
        self.quality_label.configure(text=f"{int(round(self.quality.get()))}")
        self._update_name_example()

    def _numbering_start(self) -> int:
        try:
            return max(0, int(float(self.start_num.get())))
        except Exception:
            return 1

    def _on_name_change(self):
        self._update_hints()

    def _on_lossless_change(self):
        self._sync_quality_row()
        self._update_name_example()

    def _sync_quality_row(self):
        """无损模式不需要画质滑杆。"""
        if not hasattr(self, "qrow"):
            return
        if self.lossless.get():
            self.qrow.pack_forget()
        elif not self.qrow.winfo_manager():
            self.qrow.pack(fill="x", pady=(6, 0))

    def _update_name_example(self):
        if not hasattr(self, "name_example"):
            return
        stem = Path(self.current_path).stem if self.current_path else "截图"
        start = self._numbering_start()
        total = max(1, len(self.items))
        width = max(2, len(str(start + total - 1))) if self.pad.get() else 2
        name = make_output_stem(
            stem, start, self.prefix.get(), self.suffix.get().strip(),
            bool(self.numbering.get()), bool(self.pad.get()), width
        )
        ext = (".png" if self.lossless.get()
               else (Path(self.current_path).suffix if self.current_path else ".jpg"))
        shown = name if len(name) <= 18 else name[:17] + "…"
        tip = ""
        if (self.prefix.get().strip() and not self.numbering.get()
                and len(self.items) > 1):
            tip = "（多张会重名）"
        self.name_example.configure(text=f"示例：{shown}{ext}{tip}")

    def _schedule_preview(self):
        if not self.ready:
            return
        if self._preview_job is not None:
            self.root.after_cancel(self._preview_job)
        self._preview_job = self.root.after(40, self._rebuild_preview)

    def _rebuild_preview(self):
        self._preview_job = None
        if self.src_thumb is None:
            self.preview = None
            self._render()
            return
        if self.show_before or self.work_mode.get() == "crop":
            self.preview = self.src_thumb
        else:
            self.preview = apply_mask(
                self.src_thumb, [tuple(r) for r in self.rects],
                strength=self.strength.get(), mode=self.mode.get(),
                feather=max(0, int(round(self.feather.get()))), fill=self.fill_color,
            )
        self._render()

    def _toggle_before(self, show: bool):
        """按住鼠标右键时临时显示原图，便于对比。"""
        if self.src_thumb is None or self.show_before == show:
            return
        self.show_before = show
        self._rebuild_preview()

    def _on_canvas_resize(self, _event=None):
        if self._render_job is not None:
            self.root.after_cancel(self._render_job)
        self._render_job = self.root.after(50, self._render)

    def _render(self):
        self._render_job = None
        c = self.canvas
        c.delete("all")
        cw, ch = c.winfo_width(), c.winfo_height()
        if self.preview is None or cw < 20 or ch < 20:
            c.create_text(cw // 2, ch // 2,
                          text="添加图片后即可在此预览\n拖动方框可调整打码位置",
                          fill=ORE["muted"], font=(FONT, 11), justify="center")
            self._render_zoom()
            return

        iw, ih = self.preview.size
        scale = min((cw - 8) / iw, (ch - 8) / ih)
        dw, dh = max(1, int(iw * scale)), max(1, int(ih * scale))
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        disp = self.preview if (dw, dh) == self.preview.size else \
            self.preview.resize((dw, dh), Image.LANCZOS)
        if self.work_mode.get() == "crop" and self.src_thumb is not None:
            disp = self._dim_outside_crop(disp)
        self.photo = ImageTk.PhotoImage(disp)
        c.create_image(ox, oy, anchor="nw", image=self.photo)
        self.view = (ox, oy, dw, dh)

        if self.work_mode.get() == "crop" and self.src_thumb is not None:
            iw, ih = self.preview.size
            self._draw_crop_overlay(c, ox, oy, dw, dh, iw, ih)
            self._update_crop_info()
            return

        for idx, rect in enumerate(self.rects):
            x0, y0, x1, y1 = self._rect_to_canvas(rect)
            active = idx == self.sel
            extra = {} if active else {"dash": (4, 4)}
            c.create_rectangle(x0, y0, x1, y1,
                               outline="#ffffff" if active else ORE["muted"],
                               width=2, **extra)
            if active:
                for hx, hy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                    c.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4,
                                       fill="#ffffff", outline=ORE["border"], width=2)
            c.create_text(x0 + 2, y0 - 4, anchor="sw", text=f"{idx + 1}",
                          fill="#ffffff", font=(FONT, 9, "bold"))

        self._render_zoom()

    def _render_zoom(self):
        """按原始分辨率渲染选中区域的放大图，便于确认是否真的看不清。"""
        z = self.zoom
        z.delete("all")
        cw, ch = z.winfo_width(), z.winfo_height()
        if cw < 40 or ch < 30:
            return
        self.zoom_label.configure(text="")
        if (self.src_full is None or not self.rects or self.sel is None
                or not (0 <= self.sel < len(self.rects))):
            z.create_text(14, ch // 2, anchor="w", text="选中一个打码区域即可查看放大效果",
                          fill=ORE["muted"], font=(FONT, 9))
            return

        w, h = self.src_full.size
        x0, y0, x1, y1 = _rect_to_px(self.rects[self.sel], w, h)
        radius = radius_for(w, self.strength.get())
        pad_x = max(24, int((x1 - x0) * 0.12))
        pad_y = max(6, radius // 3)
        cx0, cy0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
        cx1, cy1 = min(w, x1 + pad_x), min(h, y1 + pad_y)
        if cx1 - cx0 < 2 or cy1 - cy0 < 2:
            return

        crop = self.src_full.crop((cx0, cy0, cx1, cy1))
        rel = [((x0 - cx0) / crop.width, (y0 - cy0) / crop.height,
                (x1 - cx0) / crop.width, (y1 - cy0) / crop.height)]
        masked = apply_mask(crop, rel, strength=self.strength.get(),
                            mode=self.mode.get(),
                            feather=max(0, int(round(self.feather.get()))),
                            fill=self.fill_color, radius=radius)
        scale = min((cw - 16) / masked.width, (ch - 16) / masked.height)
        if scale <= 0:
            return
        dw = max(1, int(masked.width * scale))
        dh = max(1, int(masked.height * scale))
        disp = masked.resize((dw, dh), Image.NEAREST if scale > 1 else Image.LANCZOS)
        self.zoom_photo = ImageTk.PhotoImage(disp)
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        z.create_image(ox, oy, anchor="nw", image=self.zoom_photo)
        z.create_rectangle(ox + rel[0][0] * dw, oy + rel[0][1] * dh,
                           ox + rel[0][2] * dw, oy + rel[0][3] * dh,
                           outline="#ffffff", width=1, dash=(3, 3))
        self.zoom_label.configure(
            text=f"区域 {self.sel + 1} · {MODE_LABELS.get(self.mode.get(), '')} · "
                 f"半径/块 ≈ {radius}px · 放大 {scale:.1f}×"
        )

    # ---- 裁剪模式的叠加层与拖拽 ----
    def _draw_crop_overlay(self, c, ox, oy, dw, dh, iw, ih):
        x, y, bw, bh = self._crop_box(iw, ih)
        sx, sy = dw / max(1, iw), dh / max(1, ih)
        x0, y0 = ox + x * sx, oy + y * sy
        x1, y1 = x0 + bw * sx, y0 + bh * sy
        for i in (1, 2):
            gx = x0 + (x1 - x0) * i / 3
            gy = y0 + (y1 - y0) * i / 3
            c.create_line(gx, y0, gx, y1, fill="#ffffff", width=1, stipple="gray25")
            c.create_line(x0, gy, x1, gy, fill="#ffffff", width=1, stipple="gray25")
        c.create_rectangle(x0, y0, x1, y1, outline="#ffffff", width=2)
        for hx, hy in ((x0, y0), ((x0 + x1) / 2, y0), (x1, y0),
                       (x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2),
                       (x0, y1), ((x0 + x1) / 2, y1), (x1, y1)):
            c.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4,
                               fill="#ffffff", outline=ORE["border"], width=2)

    def _dim_outside_crop(self, disp):
        base = disp.convert("RGB")
        dim = base.point(lambda v: int(v * 0.45))
        w, h = base.size
        x, y, bw, bh = self._crop_box(w, h)
        if bw > 0 and bh > 0:
            dim.paste(base.crop((x, y, x + bw, y + bh)), (x, y))
        return dim

    def _crop_handles_canvas(self):
        ox, oy, dw, dh = self.view
        iw, ih = self.preview.size
        x, y, bw, bh = self._crop_box(iw, ih)
        sx, sy = dw / max(1, iw), dh / max(1, ih)
        x0, y0 = ox + x * sx, oy + y * sy
        x1, y1 = x0 + bw * sx, y0 + bh * sy
        return {
            "nw": (x0, y0), "n": ((x0 + x1) / 2, y0), "ne": (x1, y0),
            "w": (x0, (y0 + y1) / 2), "e": (x1, (y0 + y1) / 2),
            "sw": (x0, y1), "s": ((x0 + x1) / 2, y1), "se": (x1, y1),
            "_box": (x0, y0, x1, y1),
        }

    def _crop_press(self, event):
        if self.preview is None:
            return
        h = self._crop_handles_canvas()
        for name in ("nw", "n", "ne", "w", "e", "sw", "s", "se"):
            hx, hy = h[name]
            if abs(event.x - hx) <= 10 and abs(event.y - hy) <= 10:
                iw, ih = self.preview.size
                self.crop_drag = {"mode": "resize", "handle": name,
                                  "start": (event.x, event.y),
                                  "box": self._crop_box(iw, ih)}
                self.drag = None
                return
        x0, y0, x1, y1 = h["_box"]
        if x0 <= event.x <= x1 and y0 <= event.y <= y1:
            iw, ih = self.preview.size
            self.crop_drag = {"mode": "move", "start": (event.x, event.y),
                              "box": self._crop_box(iw, ih)}
            self.drag = None

    def _crop_motion(self, event):
        if not self.crop_drag or self.preview is None:
            return
        ox, oy, dw, dh = self.view
        iw, ih = self.preview.size
        sx, sy = dw / max(1, iw), dh / max(1, ih)
        dx = (event.x - self.crop_drag["start"][0]) / max(0.001, sx)
        dy = (event.y - self.crop_drag["start"][1]) / max(0.001, sy)
        x, y, bw, bh = self.crop_drag["box"]
        if self.crop_drag["mode"] == "move":
            nx = min(max(0.0, x + dx), iw - bw)
            ny = min(max(0.0, y + dy), ih - bh)
            self.crop_offset = [min(1.0, max(0.0, (nx + bw / 2) / iw)),
                                min(1.0, max(0.0, (ny + bh / 2) / ih))]
        else:
            nb = self._crop_resize(self.crop_drag["handle"], (x, y, bw, bh), dx, dy, iw, ih)
            self._apply_crop_box(nb, iw, ih)
        self._update_crop_info()
        self._render()

    def _crop_resize(self, handle, box, dx, dy, W, H):
        x, y, bw, bh = box
        min_size = 24
        if self.crop_ratio.get() == "custom":
            if "w" in handle:
                nx = max(0, min(x + dx, x + bw - min_size))
                bw = x + bw - nx
                x = nx
            if "e" in handle:
                bw = max(min_size, min(bw + dx, W - x))
            if "n" in handle:
                ny = max(0, min(y + dy, y + bh - min_size))
                bh = y + bh - ny
                y = ny
            if "s" in handle:
                bh = max(min_size, min(bh + dy, H - y))
            return (int(round(x)), int(round(y)), int(round(bw)), int(round(bh)))

        r = CROP_RATIOS.get(self.crop_ratio.get(), 16 / 9)
        anchor_right = "w" in handle
        anchor_bottom = "n" in handle
        bx = x + bw if anchor_right else x
        by = y + bh if anchor_bottom else y
        w = bw + (dx if "e" in handle else -dx if "w" in handle else 0)
        h = bh + (dy if "s" in handle else -dy if "n" in handle else 0)
        if "e" not in handle and "w" not in handle:
            w = h * r
        if "n" not in handle and "s" not in handle:
            h = w / r
        if len(handle) == 2:
            w = max(w, h * r)
            h = w / r
        max_w = bx if anchor_right else W - bx
        max_h = by if anchor_bottom else H - by
        limit = min(max_w, max_h * r)
        w = max(min(min_size, limit), min(w, limit))
        h = w / r
        nx = bx - w if anchor_right else bx
        ny = by - h if anchor_bottom else by
        return (int(round(nx)), int(round(ny)), int(round(w)), int(round(h)))

    def _apply_crop_box(self, box, iw, ih):
        x, y, bw, bh = box
        fw = self.full_size[0] or iw
        k = fw / max(1, iw)
        if self.crop_ratio.get() == "custom":
            self.crop_w.set(str(max(8, int(round(bw * k)))))
            self.crop_h.set(str(max(8, int(round(bh * k)))))
        else:
            mw, _mh = crop_max_for(iw, ih, self.crop_ratio.get())
            self.crop_scale = max(0.1, min(1.0, bw / max(1, mw)))
        self.crop_offset = [min(1.0, max(0.0, (x + bw / 2) / max(1, iw))),
                            min(1.0, max(0.0, (y + bh / 2) / max(1, ih)))]

    def _rect_to_canvas(self, rect):
        ox, oy, dw, dh = self.view
        return (ox + rect[0] * dw, oy + rect[1] * dh,
                ox + rect[2] * dw, oy + rect[3] * dh)

    def _canvas_to_rel(self, x, y):
        ox, oy, dw, dh = self.view
        rx = min(1.0, max(0.0, (x - ox) / max(1, dw)))
        ry = min(1.0, max(0.0, (y - oy) / max(1, dh)))
        return rx, ry

    # ---------------- 鼠标拖拽 ----------------
    def _hit_handle(self, x, y):
        if self.sel is None or not (0 <= self.sel < len(self.rects)):
            return None
        x0, y0, x1, y1 = self._rect_to_canvas(self.rects[self.sel])
        for name, (hx, hy) in (("nw", (x0, y0)), ("ne", (x1, y0)),
                               ("sw", (x0, y1)), ("se", (x1, y1))):
            if abs(x - hx) <= 8 and abs(y - hy) <= 8:
                return name
        return None

    def _rect_at(self, x, y):
        for idx in range(len(self.rects) - 1, -1, -1):
            x0, y0, x1, y1 = self._rect_to_canvas(self.rects[idx])
            if x0 - 3 <= x <= x1 + 3 and y0 - 3 <= y <= y1 + 3:
                return idx
        return None

    def _on_press(self, event):
        if self.preview is None:
            return
        self.canvas.focus_set()
        if self.work_mode.get() == "crop":
            self._crop_press(event)
            return
        handle = self._hit_handle(event.x, event.y)
        if handle:
            self.drag = {"type": "resize", "handle": handle}
            return
        idx = self._rect_at(event.x, event.y)
        if idx is not None:
            if idx != self.sel:
                self.sel = idx
                self._sync_region_selection()
                self._sync_entries()
                self._schedule_preview()
            self.drag = {"type": "move", "start": (event.x, event.y),
                         "rect": list(self.rects[idx])}
            return
        rx, ry = self._canvas_to_rel(event.x, event.y)
        self.drag = {"type": "new", "start": (rx, ry), "temp_index": None}
        self.sel = None
        self._sync_region_selection()

    def _on_motion(self, event):
        if self.work_mode.get() == "crop":
            self._crop_motion(event)
            return
        if not self.drag:
            return
        kind = self.drag["type"]
        if kind == "move":
            _, _, dw, dh = self.view
            dx = (event.x - self.drag["start"][0]) / max(1, dw)
            dy = (event.y - self.drag["start"][1]) / max(1, dh)
            x0, y0, x1, y1 = self.drag["rect"]
            dx = min(max(dx, -x0), 1 - x1)
            dy = min(max(dy, -y0), 1 - y1)
            self.rects[self.sel] = [x0 + dx, y0 + dy, x1 + dx, y1 + dy]
        elif kind == "resize":
            rx, ry = self._canvas_to_rel(event.x, event.y)
            r = list(self.rects[self.sel])
            h = self.drag["handle"]
            if "w" in h:
                r[0] = min(rx, r[2] - 0.004)
            if "e" in h:
                r[2] = max(rx, r[0] + 0.004)
            if "n" in h:
                r[1] = min(ry, r[3] - 0.004)
            if "s" in h:
                r[3] = max(ry, r[1] + 0.004)
            self.rects[self.sel] = r
        else:  # new
            rx, ry = self._canvas_to_rel(event.x, event.y)
            sx, sy = self.drag["start"]
            new_rect = [min(sx, rx), min(sy, ry), max(sx, rx), max(sy, ry)]
            idx = self.drag.get("temp_index")
            if idx is None or idx >= len(self.rects):
                self.rects.append(new_rect)
                idx = self.drag["temp_index"] = len(self.rects) - 1
            else:
                self.rects[idx] = new_rect
            self.sel = idx

        self._sync_entries()
        self._schedule_preview()

    def _on_release(self, _event):
        if self.crop_drag:
            self.crop_drag = None
            return
        if not self.drag:
            return
        if self.drag["type"] == "new":
            idx = self.drag.get("temp_index")
            if idx is not None and idx < len(self.rects):
                r = self.rects[idx]
                if r[2] - r[0] < 0.008 or r[3] - r[1] < 0.008:
                    del self.rects[idx]
                    self.sel = min(idx, len(self.rects) - 1) if self.rects else None
            if self.rects and self.sel is None:
                self.sel = 0
            self._refresh_regions_list()
        self.drag = None
        self._sync_entries()
        self._schedule_preview()

    def _on_double_click(self, event):
        idx = self._rect_at(event.x, event.y)
        if idx is not None:
            self.sel = idx
            self._sync_region_selection()
            self._sync_entries()
            self._schedule_preview()

    # ---------------- 区域列表 / 输入框 ----------------
    def _refresh_regions_list(self):
        self.regions.delete(0, "end")
        for r in self.rects:
            x0, y0, x1, y1 = r
            self.regions.insert("end",
                                f"{x0 * 100:5.1f}% , {y0 * 100:4.1f}%   →   "
                                f"{x1 * 100:5.1f}% , {y1 * 100:4.1f}%")
        self._sync_region_selection()

    def _sync_region_selection(self):
        self.regions.selection_clear(0, "end")
        if self.sel is not None and 0 <= self.sel < len(self.rects):
            self.regions.selection_set(self.sel)
            self.regions.see(self.sel)

    def _on_region_select(self, _event=None):
        sel = self.regions.curselection()
        if not sel:
            return
        self.sel = int(sel[0])
        self._sync_entries()
        self._schedule_preview()

    def _sync_entries(self):
        if self.sel is None or not (0 <= self.sel < len(self.rects)):
            for v in self.entry_vars.values():
                v.set("")
            return
        x0, y0, x1, y1 = self.rects[self.sel]
        self.entry_vars["x"].set(f"{x0 * 100:.2f}")
        self.entry_vars["y"].set(f"{y0 * 100:.2f}")
        self.entry_vars["w"].set(f"{(x1 - x0) * 100:.2f}")
        self.entry_vars["h"].set(f"{(y1 - y0) * 100:.2f}")
        self._refresh_regions_list()

    def _apply_entries(self, _event=None):
        if self.sel is None or not (0 <= self.sel < len(self.rects)):
            return
        try:
            x = float(self.entry_vars["x"].get())
            y = float(self.entry_vars["y"].get())
            w = float(self.entry_vars["w"].get())
            h = float(self.entry_vars["h"].get())
        except ValueError:
            self._sync_entries()
            return
        x0 = min(max(x / 100.0, 0.0), 1.0)
        y0 = min(max(y / 100.0, 0.0), 1.0)
        w = min(max(w / 100.0, 0.002), 1.0 - x0)
        h = min(max(h / 100.0, 0.002), 1.0 - y0)
        self.rects[self.sel] = [x0, y0, x0 + w, y0 + h]
        self._refresh_regions_list()
        self._schedule_preview()

    def add_region(self):
        base = self.rects[-1] if self.rects else list(DEFAULT_RECT)
        w = max(0.06, base[2] - base[0])
        h = max(0.02, base[3] - base[1])
        cx = (base[0] + base[2]) / 2
        cy = max(0.0, min(1.0 - h, (base[1] + base[3]) / 2 - 0.12))
        self.rects.append([cx - w / 2, cy, cx + w / 2, cy + h])
        self.sel = len(self.rects) - 1
        self._refresh_regions_list()
        self._sync_entries()
        self._schedule_preview()

    def del_region(self):
        if not self.rects:
            return
        idx = self.sel if self.sel is not None else len(self.rects) - 1
        idx = min(max(idx, 0), len(self.rects) - 1)
        del self.rects[idx]
        self.sel = min(idx, len(self.rects) - 1) if self.rects else None
        self._refresh_regions_list()
        self._sync_entries()
        self._schedule_preview()

    def reset_region(self):
        self.rects = [list(DEFAULT_RECT)]
        self.sel = 0
        self._refresh_regions_list()
        self._sync_entries()
        self._schedule_preview()

    # ---------------- 输出 ----------------
    def choose_outdir(self):
        from tkinter import filedialog

        folder = filedialog.askdirectory(title="选择输出目录")
        if folder:
            self.outdir.set(folder)

    def open_outdir(self):
        path = self.outdir.get().strip()
        if not path and self.items:
            path = str(self.items[0][0].parent / "打码输出")
        if path and Path(path).is_dir():
            os.startfile(path)
        else:
            from tkinter import messagebox

            messagebox.showinfo("提示", "输出目录还不存在，先批量处理一次吧。")

    def _log(self, text, tag="info"):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------- 批量处理 ----------------
    def start_batch(self):
        from tkinter import messagebox

        if self.worker is not None and self.worker.is_alive():
            return
        if not self.items:
            messagebox.showwarning("还没有图片", "请先添加要打码的图片或文件夹。")
            return
        crop_mode = self.work_mode.get() == "crop"
        if not crop_mode and not self.rects:
            messagebox.showwarning("没有打码区域", "请先在预览图上画出至少一个打码区域。")
            return

        outdir_text = self.outdir.get().strip()
        if not outdir_text:
            outdir_text = str(self.items[0][0].parent / "打码输出")
            self.outdir.set(outdir_text)
        outdir = Path(outdir_text)

        suffix = self.suffix.get().strip()
        jobs = build_jobs(self.items, outdir, suffix, self.prefix.get().strip(),
                          bool(self.numbering.get()), self._numbering_start(),
                          bool(self.pad.get()), bool(self.lossless.get()))
        if crop_mode:
            opts = dict(crop=dict(ratio=self.crop_ratio.get(),
                                  scale=self.crop_scale,
                                  w=self._num(self.crop_w, 1920),
                                  h=self._num(self.crop_h, 1080),
                                  ox=self.crop_offset[0], oy=self.crop_offset[1]),
                        quality=self.quality.get())
        else:
            self.rects = [[float(v) for v in r] for r in self.rects]
            self._refresh_regions_list()
            opts = dict(rects=[tuple(r) for r in self.rects], strength=self.strength.get(),
                        mode=self.mode.get(),
                        feather=max(0, int(round(self.feather.get()))),
                        fill=self.fill_color, quality=self.quality.get())

        self.stop_event.clear()
        self.stats = {"ok": 0, "err": 0, "skip": 0}
        self._batch_total = max(1, len(jobs))
        self.progress.set_max(self._batch_total)
        self.progress.set(0)
        self.btn_start.set_state("disabled")
        self.btn_stop.set_state("normal")
        self._log(f"开始处理 {len(jobs)} 张 → {outdir}", "info")
        self.status.configure(text=f"正在处理 0/{len(jobs)} …")

        self.worker = threading.Thread(target=self._run_batch,
                                       args=(jobs, opts, len(jobs)), daemon=True)
        self.worker.start()
        self.root.after(80, self._poll)

    def _run_batch(self, jobs, opts, total):
        try:
            with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 2)) as ex:
                futures = {ex.submit(process_one, src, dst, **opts): (src, dst)
                           for src, dst in jobs}
                for fut in as_completed(futures):
                    src, dst = futures[fut]
                    if self.stop_event.is_set():
                        self.q.put(("skip", src, str(dst)))
                        continue
                    try:
                        fut.result()
                        self.q.put(("ok", src, str(dst)))
                    except Exception as exc:
                        self.q.put(("err", src, f"{type(exc).__name__}: {exc}"))
        except Exception as exc:  # 兜底
            self.q.put(("fatal", None, f"{type(exc).__name__}: {exc}"))
        finally:
            self.q.put(("end", None, total))

    def _poll(self):
        finished = False
        total = self._batch_total
        st = self.stats
        while True:
            try:
                kind, src, info = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "ok":
                st["ok"] += 1
                self.progress.set(st["ok"] + st["err"] + st["skip"])
                self.status.configure(
                    text=f"正在处理 {st['ok'] + st['err']}/{total} … {src.name}")
            elif kind == "err":
                st["err"] += 1
                self.progress.set(st["ok"] + st["err"] + st["skip"])
                self._log(f"失败：{src.name} — {info}", "err")
            elif kind == "skip":
                st["skip"] += 1
                self.progress.set(st["ok"] + st["err"] + st["skip"])
                self._log(f"跳过：{src.name}", "info")
            elif kind == "fatal":
                st["err"] += 1
                self._log(f"严重错误：{info}", "err")
            elif kind == "end":
                finished = True
        if finished:
            self._finish_batch(st["ok"], st["err"], st["skip"], total)
        else:
            self.root.after(80, self._poll)

    def _finish_batch(self, ok, err, skip, total):
        from tkinter import messagebox

        self.btn_start.set_state("normal")
        self.btn_stop.set_state("disabled")
        self.progress.set(total)
        self.status.configure(text=f"完成：成功 {ok} 张，失败 {err} 张，跳过 {skip} 张")
        self._log(f"完成：成功 {ok} 张，失败 {err} 张，跳过 {skip} 张",
                  "ok" if not err else "err")
        self.worker = None
        msg = f"成功 {ok} 张，失败 {err} 张"
        if skip:
            msg += f"，跳过 {skip} 张"
        messagebox.showinfo("批量打码完成", msg + f"\n\n输出目录：\n{self.outdir.get()}")

    def stop_batch(self):
        self.stop_event.set()
        self._log("已请求停止，正在结束当前文件…", "info")
        self.btn_stop.set_state("disabled")


# --------------------------------------------------------------------------- #
# 命令行模式
# --------------------------------------------------------------------------- #
def run_cli(args) -> int:
    inputs = [Path(p) for p in args.input]
    items, skipped = collect_inputs(inputs, recursive=not args.no_recursive,
                                    suffix=args.suffix)
    if not items:
        print("没有找到可处理的图片。", file=sys.stderr)
        return 2

    rects = [parse_rect(t) for t in args.rect] if args.rect else [DEFAULT_RECT]

    crop = None
    if args.crop_size:
        try:
            cw, ch = [int(v) for v in str(args.crop_size).lower().replace("x", ",").split(",")]
            crop = dict(ratio="custom", w=cw, h=ch, ox=0.5, oy=0.5)
        except Exception:
            print("--crop-size 格式应为 宽x高，例如 1920x1080", file=sys.stderr)
            return 2
    elif args.crop_ratio:
        try:
            ox, oy = [float(v) for v in str(args.crop_pos).split(",")]
        except Exception:
            ox, oy = 0.5, 0.5
        crop = dict(ratio=args.crop_ratio, scale=args.crop_scale, ox=ox, oy=oy)

    suffix = args.suffix
    if crop and suffix == "_已打码":
        suffix = "_裁剪"

    outdir = Path(args.output) if args.output else items[0][0].parent / "打码输出"
    jobs = build_jobs(items, outdir, suffix, args.prefix, args.numbering,
                      args.start, args.pad, not args.no_lossless)
    print(f"共 {len(jobs)} 张，输出到：{outdir}")
    if crop:
        print(f"裁剪：{'自定义 %d×%d' % (crop['w'], crop['h']) if crop['ratio'] == 'custom' else crop['ratio']}"
              f"  位置：{crop['ox']}, {crop['oy']}")
    else:
        print(f"区域：{rects}  模式：{MODE_LABELS.get(args.mode, args.mode)}  "
              f"强度：{args.strength}")

    ok = err = 0
    for src, dst in jobs:
        try:
            if crop:
                process_one(src, dst, crop=crop, quality=args.quality)
            else:
                process_one(src, dst, rects=rects, strength=args.strength, mode=args.mode,
                            feather=args.feather, fill=parse_color(args.fill),
                            quality=args.quality)
            ok += 1
            print(f"  [OK] {src.name} -> {dst}")
        except Exception as exc:
            err += 1
            print(f"  [ERR] {src.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"完成：成功 {ok}，失败 {err}，跳过 {skipped}")
    return 0 if err == 0 else 1


def build_arg_parser():
    p = argparse.ArgumentParser(description="截图 UID 批量打码工具（高斯模糊 / 马赛克）")
    p.add_argument("--cli", action="store_true", help="命令行模式，不打开界面")
    p.add_argument("-i", "--input", nargs="+", default=[], help="图片文件或文件夹")
    p.add_argument("-o", "--output", default="",
                   help="输出目录（默认：第一张图片所在目录/打码输出）")
    p.add_argument("--strength", type=float, default=7.0,
                   help="模糊强度：每 1000px 宽度的模糊半径，默认 7")
    p.add_argument("--mode", default="gaussian", choices=["gaussian", "mosaic", "solid"],
                   help="打码方式，默认 gaussian")
    p.add_argument("--feather", type=int, default=2, help="边缘羽化像素，默认 2")
    p.add_argument("--fill", default="#12141a",
                   help="纯色遮盖的颜色，支持 #rrggbb / rgb(r,g,b)，默认 #12141a")
    p.add_argument("--rect", action="append",
                   help="打码区域 x0,y0,x1,y1（0~1 相对坐标，可重复）；默认左下角 UID 区域")
    p.add_argument("--suffix", default="_已打码", help="输出文件名后缀，默认 _已打码")
    p.add_argument("--prefix", default="", help="输出文件名前缀（留空则沿用原文件名）")
    p.add_argument("--numbering", action="store_true",
                   help="按顺序在文件名末尾加 1、2、3…（上传顺序）")
    p.add_argument("--start", type=int, default=1, help="编号起始值，默认 1")
    p.add_argument("--pad", action="store_true", help="编号补零（01、02…）")
    p.add_argument("--quality", type=int, default=95, help="JPG/WebP 画质，默认 95")
    p.add_argument("--no-lossless", action="store_true",
                   help="按原格式保存（默认输出无损 PNG）")
    p.add_argument("--crop-ratio", choices=list(CROP_RATIOS.keys()),
                   help="批量裁剪为指定比例（16:9 / 4:3 / 1:1 / 9:16 / 3:4）")
    p.add_argument("--crop-size", metavar="WxH",
                   help="批量裁剪为指定像素，如 1920x1080")
    p.add_argument("--crop-scale", type=float, default=1.0,
                   help="比例模式下裁剪框缩放，0.1~1，默认 1（最大）")
    p.add_argument("--crop-pos", default="0.5,0.5",
                   help="裁剪框中心位置 0~1，默认 0.5,0.5（居中）")
    p.add_argument("--no-recursive", action="store_true", help="文件夹不递归子目录")
    return p


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_arg_parser().parse_args(argv)
    if args.cli or args.input:
        return run_cli(args)
    return run_gui()


def run_gui() -> int:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    import tkinter as tk

    root = tk.Tk()
    app = BlurApp(root)
    root.app = app  # 方便脚本调试
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
