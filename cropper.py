#!/usr/bin/env python3
"""
Kash Cropper - Video Cropping & Clipping Tool

Crop tab  : Draw rectangles on a preview frame → export each region as a
            separate video file.

Clip tab  : Load multiple videos, scrub frame-by-frame, set in/out points,
            then export trimmed versions into a "clipped" sub-folder next to
            the source files.
"""

import concurrent.futures
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import winsound          # Windows-only; completion chime no-ops elsewhere
except ImportError:
    winsound = None

import cv2
import numpy as np
from PIL import Image, ImageTk


# ---------------------------------------------------------------------------
# Shared constants / helpers
# ---------------------------------------------------------------------------
APP_NAME = "Kash Cropper"
VERSION = "1.1.0"

COLORS = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#FFA07A", "#98D8C8",
    "#F7DC6F", "#BB8FCE", "#85C1E9", "#82E0AA", "#F0B27A",
    "#FF8C94", "#A8E6CF", "#FFD3B6", "#D4A5A5", "#9BC1BC",
    "#5E8C61", "#F18F01", "#C73E1D", "#3B1F2B", "#44BBA4",
]

FOURCC_MAP = {
    ".mp4":  "mp4v",
    ".avi":  "XVID",
    ".mov":  "mp4v",
    ".mkv":  "mp4v",
    ".mpg":  "PIM1",
    ".mpeg": "PIM1",
    ".wmv":  "WMV2",
    ".flv":  "FLV1",
    ".webm": "VP80",
    ".m4v":  "mp4v",
}

VIDEO_FILETYPES = [
    ("Video files", "*.mp4 *.avi *.mov *.mkv *.mpg *.mpeg *.wmv *.flv *.webm *.m4v"),
    ("All files", "*.*"),
]

BG      = "#1e1e2e"
BG_DARK = "#11111b"
BG_MID  = "#313244"
FG      = "#cdd6f4"
FG_DIM  = "#6c7086"
ACCENT  = "#89b4fa"
GREEN   = "#a6e3a1"
RED     = "#f38ba8"
YELLOW  = "#f9e2af"
PURPLE  = "#cba6f7"


def _fourcc(ext: str):
    return cv2.VideoWriter_fourcc(*FOURCC_MAP.get(ext.lower(), "mp4v"))


def _frames_to_tc(n: int, fps: float) -> str:
    """Format a frame index as  HH:MM:SS;FF"""
    if fps <= 0:
        fps = 30.0
    total_s = int(n / fps)
    ff = n % max(1, int(fps))
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d};{ff:02d}"


def _separator(parent) -> None:
    tk.Frame(parent, bg=BG_MID, height=1).pack(fill=tk.X, padx=8, pady=3)


def _folder_of(path: str) -> str:
    """Return the absolute folder a file lives in (for the queue 'Folder' column)."""
    return os.path.dirname(os.path.abspath(path))


def _shift_rect_clamped(rect, dox, doy, frame_shape):
    """Translate an (x1, y1, x2, y2) rect by (dox, doy) in original-pixel space,
    keeping its size fixed and clamping it inside the frame bounds.

    `frame_shape` is the numpy (height, width) tuple of the source frame. Used
    by the crop tabs' "Move" mode to drag a region without resizing it.
    """
    oh, ow = frame_shape[0], frame_shape[1]
    x1, y1, x2, y2 = rect
    w, h = x2 - x1, y2 - y1
    nx1 = max(0, min(x1 + dox, ow - w))
    ny1 = max(0, min(y1 + doy, oh - h))
    return (nx1, ny1, nx1 + w, ny1 + h)


def _build_queue_tree(wrap, scrollbar, extra_cols=()):
    """Create a dark-themed, multi-select file-queue Treeview inside `wrap`.

    Columns are always  File (#0)  +  Folder, plus any (key, label, width)
    tuples in `extra_cols`. Rows are inserted with iid=str(index); read the
    current selection with tree.selection() (returns those iids). The Folder
    column lets the user tell apart same-named files from different locations,
    and headings are drag-resizable. Returns the Treeview.
    """
    cols = ("folder",) + tuple(c[0] for c in extra_cols)
    tree = ttk.Treeview(
        wrap, columns=cols, show="tree headings", style="Queue.Treeview",
        yscrollcommand=scrollbar.set, selectmode="extended")
    tree.heading("#0", text="File")
    tree.heading("folder", text="Folder")
    tree.column("#0", width=150, anchor="w", stretch=True)
    tree.column("folder", width=150, anchor="w", stretch=True)
    for key, label, width in extra_cols:
        tree.heading(key, text=label)
        tree.column(key, width=width, anchor="center", stretch=False)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.config(command=tree.yview)
    return tree


def _ffmpeg_available() -> bool:
    """Return True if ffmpeg is reachable on PATH."""
    return shutil.which("ffmpeg") is not None


def _nvenc_available() -> bool:
    """Return True if NVIDIA NVENC encoders are available in ffmpeg."""
    if not _ffmpeg_available():
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        )
        return b"h264_nvenc" in result.stdout
    except Exception:
        return False


# Output format options: label → (ffmpeg_args_list, output_extension)
# -pix_fmt yuv420p  → required by Windows built-in H.264/H.265 decoder
# -profile:v main   → broadest compatibility with Windows / mobile players
# -movflags +faststart → puts the MOOV atom at the front for quick playback
_H264_COMPAT = ["-pix_fmt", "yuv420p", "-profile:v", "main", "-movflags", "+faststart"]
_H265_COMPAT = ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]

OUTPUT_FORMATS = {
    "MP4 — H.264 ultrafast (fastest)": (
        ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18"] + _H264_COMPAT + ["-c:a", "aac"], ".mp4"),
    "MP4 — H.264 fast":                (
        ["-c:v", "libx264", "-preset", "fast",      "-crf", "18"] + _H264_COMPAT + ["-c:a", "aac"], ".mp4"),
    "MP4 — H.265 / HEVC":              (
        ["-c:v", "libx265", "-preset", "fast",      "-crf", "22"] + _H265_COMPAT + ["-c:a", "aac"], ".mp4"),
}

# Add NVIDIA NVENC GPU-accelerated formats if the GPU encoder is available
if _nvenc_available():
    OUTPUT_FORMATS["MP4 — H.264 NVENC (GPU, fastest)"] = (
        ["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "constqp", "-qp", "18"]
        + _H264_COMPAT + ["-c:a", "aac"], ".mp4")
    OUTPUT_FORMATS["MP4 — H.264 NVENC (GPU, quality)"] = (
        ["-c:v", "h264_nvenc", "-preset", "p7", "-rc", "constqp", "-qp", "18"]
        + _H264_COMPAT + ["-c:a", "aac"], ".mp4")
    OUTPUT_FORMATS["MP4 — H.265 NVENC (GPU)"] = (
        ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "constqp", "-qp", "22"]
        + _H265_COMPAT + ["-c:a", "aac"], ".mp4")

# Clip tab gets an extra stream-copy option (no re-encode = no quality loss)
CLIP_FORMATS = {
    "MP4 — stream copy (no re-encode)": (["-c", "copy"], ".mp4"),
    **OUTPUT_FORMATS,
}
CROP_FORMAT_NAMES = list(OUTPUT_FORMATS.keys())
CLIP_FORMAT_NAMES = list(CLIP_FORMATS.keys())


def _run_ffmpeg(args: list) -> None:
    """Run ffmpeg silently; raise RuntimeError with the real error on failure.

    -hide_banner drops the version/build banner, and on failure we surface the
    tail of stderr (where ffmpeg prints the actual error) instead of the head.
    """
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y"] + args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        err = result.stderr.decode(errors="replace")
        lines = [ln for ln in err.splitlines() if ln.strip()]
        raise RuntimeError("\n".join(lines[-4:]) if lines else "ffmpeg failed (no output).")


def _has_audio(path: str) -> bool:
    """Return True if `path` has at least one audio stream (via ffprobe).

    Used by the Cut & Stitch exporter to decide whether to build the audio
    half of the concat filter. If ffprobe is missing or errors, assume no
    audio so the video-only filter path is used (which always succeeds).
    """
    exe = shutil.which("ffprobe")
    if not exe:
        return False
    try:
        result = subprocess.run(
            [exe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Completion chime (cash-register sound played when a job finishes)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CASH_MP3 = os.path.join(_SCRIPT_DIR, "CashRegisterNoise.mp3")
# winsound only plays WAV, so we cache a one-time MP3→WAV decode here.
_CASH_WAV = os.path.join(_SCRIPT_DIR, "__pycache__", "CashRegisterNoise.wav")


def _ensure_cash_wav():
    """Decode the cash-register MP3 to a cached WAV once; return its path or None.

    winsound can only play WAV files, so the bundled MP3 is transcoded with
    ffmpeg the first time and reused thereafter. Safe to call from any thread.
    """
    if winsound is None:
        return None
    if os.path.isfile(_CASH_WAV):
        return _CASH_WAV
    if not os.path.isfile(_CASH_MP3) or not _ffmpeg_available():
        return None
    try:
        os.makedirs(os.path.dirname(_CASH_WAV), exist_ok=True)
        _run_ffmpeg(["-i", _CASH_MP3, _CASH_WAV])
        return _CASH_WAV
    except Exception:
        return None


def _play_cash_register() -> None:
    """Play the cash-register chime asynchronously. Never raises — a missing
    file, absent ffmpeg, or unavailable audio device is silently ignored."""
    if winsound is None:
        return
    wav = _ensure_cash_wav()
    if not wav:
        return
    try:
        winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:
        pass


def _measure_luma(path: str, total: int = 0) -> float:
    """Return mean Y-channel luma (0–255) for a video file.
    Samples 3 evenly-spaced frames from the middle third.
    I/O-bound — safe to call from a ThreadPoolExecutor worker.
    """
    if total <= 0:
        cap_tmp = cv2.VideoCapture(path)
        total = int(cap_tmp.get(cv2.CAP_PROP_FRAME_COUNT)) or 30
        cap_tmp.release()
    lo = total // 3
    hi = max(lo + 3, (total * 2) // 3)
    hi = min(hi, total - 1)
    span = max(hi - lo, 1)
    indices = [lo + span // 4, lo + span // 2, lo + (span * 3) // 4]
    cap = cv2.VideoCapture(path)
    lumas = []
    for fi in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, frame = cap.read()
        if not ok:
            continue
        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        lumas.append(float(np.mean(ycrcb[:, :, 0])))
    cap.release()
    return float(np.mean(lumas)) if lumas else 128.0


# ════════════════════════════════════════════════════════════════════════════
# Crop Tab
# ════════════════════════════════════════════════════════════════════════════

class CropTab:
    """Region-crop tool: draw rectangles on a preview frame and export each."""

    def __init__(self, parent: tk.Frame, root: tk.Tk) -> None:
        self.parent = parent
        self.root = root

        self.video_path: str = ""
        self.orig_frame = None
        self.photo_img = None

        self.scale_x: float = 1.0
        self.scale_y: float = 1.0
        self.off_x: int = 0
        self.off_y: int = 0

        self.crops: list = []
        self._drawing = False
        self._drag_start = (0, 0)
        self._drag_rect_id = None

        # Interaction mode: "draw" (rubber-band a new rect) or "move" (drag an
        # existing rect around). Toggled by the Mode button.
        self._mode = "draw"
        self._moving = False
        self._move_idx = -1
        self._move_anchor = (0, 0)
        self._move_orig_rect = None

        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        bar = tk.Frame(self.parent, bg=BG_DARK, pady=7)
        bar.pack(fill=tk.X)

        tk.Button(
            bar, text="  Open Video  ", command=self._open_video,
            bg=ACCENT, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=4,
        ).pack(side=tk.LEFT, padx=10)

        self._mode_btn = tk.Button(
            bar, text="  Mode: Draw ✎  ", command=self._toggle_mode,
            bg=PURPLE, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=4,
        )
        self._mode_btn.pack(side=tk.LEFT, padx=4)

        self._vid_lbl = tk.Label(
            bar, text="No video loaded", bg=BG_DARK, fg=FG, font=("Segoe UI", 10),
        )
        self._vid_lbl.pack(side=tk.LEFT, padx=6)

        tk.Button(
            bar, text="  Clear All  ", command=self._clear_all,
            bg=RED, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=4,
        ).pack(side=tk.RIGHT, padx=10)

        body = tk.Frame(self.parent, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        cframe = tk.Frame(body, bg=BG_MID, bd=1, relief=tk.SOLID)
        cframe.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(cframe, bg=BG_DARK, cursor="crosshair", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Configure>",       lambda _e: self._render_frame())

        panel = tk.Frame(body, bg=BG_DARK, width=285)
        panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        panel.pack_propagate(False)

        tk.Label(panel, text="Output File Names",
                 bg=BG_DARK, fg=FG, font=("Segoe UI", 12, "bold")).pack(pady=(14, 2))
        tk.Label(panel, text="without extension  •  must be unique",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 8)).pack(pady=(0, 8))

        list_outer = tk.Frame(panel, bg=BG_DARK)
        list_outer.pack(fill=tk.BOTH, expand=True, padx=8)
        sb = tk.Scrollbar(list_outer, orient=tk.VERTICAL)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._list_cv = tk.Canvas(list_outer, bg=BG_DARK, yscrollcommand=sb.set, highlightthickness=0)
        self._list_cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=self._list_cv.yview)
        self._list_inner = tk.Frame(self._list_cv, bg=BG_DARK)
        self._list_win = self._list_cv.create_window((0, 0), window=self._list_inner, anchor="nw")
        self._list_inner.bind("<Configure>", lambda _: self._list_cv.configure(
            scrollregion=self._list_cv.bbox("all")))
        self._list_cv.bind("<Configure>", lambda e: self._list_cv.itemconfig(
            self._list_win, width=e.width))

        # Format selector
        _separator(panel)
        fmt_row = tk.Frame(panel, bg=BG_DARK)
        fmt_row.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(fmt_row, text="Output Format:", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._crop_fmt_var = tk.StringVar(value=CROP_FORMAT_NAMES[0])
        ttk.Combobox(fmt_row, textvariable=self._crop_fmt_var,
                     values=CROP_FORMAT_NAMES, state="readonly",
                     font=("Segoe UI", 9)).pack(fill=tk.X, pady=(2, 0))

        self._progress = tk.DoubleVar(value=0)
        ttk.Progressbar(panel, variable=self._progress, maximum=100,
                        style="Crop.Horizontal.TProgressbar", length=265
                        ).pack(padx=10, pady=(6, 3))

        tk.Button(panel, text="  START CROP", command=self._start_crop_thread,
                  bg=GREEN, fg=BG_DARK, font=("Segoe UI", 13, "bold"),
                  relief=tk.FLAT, pady=11).pack(fill=tk.X, padx=10, pady=(3, 8))

        self._status = tk.StringVar(value="Open a video to begin.")
        tk.Label(panel, textvariable=self._status, bg=BG_DARK, fg=FG_DIM,
                 font=("Segoe UI", 9), wraplength=255, justify=tk.LEFT
                 ).pack(padx=10, anchor="w")

        self.root.after(150, self._draw_placeholder)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _draw_placeholder(self) -> None:
        self.canvas.delete("placeholder")
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.create_text(w // 2, h // 2, text="Open a video file to begin",
                                fill="#45475a", font=("Segoe UI", 18), tags="placeholder")

    def _color(self, idx: int) -> str:
        return COLORS[idx % len(COLORS)]

    # ── Video loading ─────────────────────────────────────────────────────────

    def _open_video(self) -> None:
        path = filedialog.askopenfilename(title="Select a video file", filetypes=VIDEO_FILETYPES)
        if not path:
            return
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("Error", f"Cannot open video:\n{path}")
            return
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            messagebox.showerror("Error", "Video has no readable frames.")
            cap.release()
            return
        lo, hi = total // 3, (total * 2) // 3
        target = random.randint(lo, hi) if hi > lo else max(0, total // 2)
        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ok, raw = cap.read()
        cap.release()
        if not ok or raw is None:
            messagebox.showerror("Error", "Could not decode a frame from the video.")
            return
        self.video_path = path
        self.orig_frame = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
        self._vid_lbl.config(text=os.path.basename(path))
        self._clear_all(keep_video=True)
        self._render_frame()
        self._status.set("Click and drag to draw crop regions.")

    # ── Frame rendering ───────────────────────────────────────────────────────

    def _render_frame(self) -> None:
        if self.orig_frame is None:
            self._draw_placeholder()
            return
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 4 or ch < 4:
            self.root.after(60, self._render_frame)
            return
        oh, ow = self.orig_frame.shape[:2]
        ratio = min(cw / ow, ch / oh)
        nw, nh = int(ow * ratio), int(oh * ratio)
        self.scale_x = ow / nw
        self.scale_y = oh / nh
        self.off_x = (cw - nw) // 2
        self.off_y = (ch - nh) // 2
        img = Image.fromarray(self.orig_frame).resize((nw, nh), Image.LANCZOS)
        self.photo_img = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(self.off_x, self.off_y, anchor=tk.NW, image=self.photo_img)
        for idx, crop in enumerate(self.crops):
            self._draw_crop_overlay(idx, crop)

    # ── Coordinate helpers ────────────────────────────────────────────────────

    def _canvas_to_orig(self, cx, cy):
        oh, ow = self.orig_frame.shape[:2]
        return (max(0, min(int((cx - self.off_x) * self.scale_x), ow)),
                max(0, min(int((cy - self.off_y) * self.scale_y), oh)))

    def _orig_to_canvas(self, ox, oy):
        return ox / self.scale_x + self.off_x, oy / self.scale_y + self.off_y

    # ── Mode toggle ───────────────────────────────────────────────────────────

    def _toggle_mode(self) -> None:
        self._mode = "move" if self._mode == "draw" else "draw"
        if self._mode == "move":
            self._mode_btn.config(text="  Mode: Move ✥  ")
            self.canvas.config(cursor="fleur")
            self._status.set("Move mode: drag a crop region to reposition it.")
        else:
            self._mode_btn.config(text="  Mode: Draw ✎  ")
            self.canvas.config(cursor="crosshair")
            self._status.set("Draw mode: click and drag to add a crop region.")

    def _crop_at(self, cx, cy):
        """Return the index of the topmost crop whose box contains (cx, cy)."""
        for idx in range(len(self.crops) - 1, -1, -1):
            ox1, oy1, ox2, oy2 = self.crops[idx]["rect"]
            bx1, by1 = self._orig_to_canvas(ox1, oy1)
            bx2, by2 = self._orig_to_canvas(ox2, oy2)
            if min(bx1, bx2) <= cx <= max(bx1, bx2) and min(by1, by2) <= cy <= max(by1, by2):
                return idx
        return -1

    # ── Mouse events ──────────────────────────────────────────────────────────

    def _on_press(self, event) -> None:
        if self.orig_frame is None:
            return
        if self._mode == "move":
            idx = self._crop_at(event.x, event.y)
            if idx < 0:
                return
            self._moving = True
            self._move_idx = idx
            self._move_anchor = (event.x, event.y)
            self._move_orig_rect = self.crops[idx]["rect"]
            return
        self._drawing = True
        self._drag_start = (event.x, event.y)
        self._drag_rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#ffffff", width=2, dash=(5, 3))

    def _on_drag(self, event) -> None:
        if self._moving:
            self._drag_move(event)
            return
        if self._drawing and self._drag_rect_id is not None:
            x0, y0 = self._drag_start
            self.canvas.coords(self._drag_rect_id, x0, y0, event.x, event.y)

    def _drag_move(self, event) -> None:
        ax, ay = self._move_anchor
        dox = int((event.x - ax) * self.scale_x)
        doy = int((event.y - ay) * self.scale_y)
        new_rect = _shift_rect_clamped(self._move_orig_rect, dox, doy, self.orig_frame.shape[:2])
        crop = self.crops[self._move_idx]
        crop["rect"] = new_rect
        for cid in crop.get("canvas_ids", ()):
            self.canvas.delete(cid)
        self._draw_crop_overlay(self._move_idx, crop)

    def _on_release(self, event) -> None:
        if self._moving:
            self._moving = False
            self._move_idx = -1
            self._move_orig_rect = None
            return
        if not self._drawing:
            return
        self._drawing = False
        if self._drag_rect_id is not None:
            self.canvas.delete(self._drag_rect_id)
            self._drag_rect_id = None
        x0, y0 = self._drag_start
        cx1, cy1 = min(x0, event.x), min(y0, event.y)
        cx2, cy2 = max(x0, event.x), max(y0, event.y)
        if (cx2 - cx1) < 10 or (cy2 - cy1) < 10:
            return
        ox1, oy1 = self._canvas_to_orig(cx1, cy1)
        ox2, oy2 = self._canvas_to_orig(cx2, cy2)
        idx = len(self.crops)
        name_var = tk.StringVar(value=f"crop_{idx + 1}")
        crop = {"rect": (ox1, oy1, ox2, oy2), "name_var": name_var, "canvas_ids": ()}
        self.crops.append(crop)
        self._draw_crop_overlay(idx, crop)
        self._add_name_row(idx, name_var)
        self._status.set(f"{len(self.crops)} crop region(s) defined.")

    # ── Overlay ───────────────────────────────────────────────────────────────

    def _draw_crop_overlay(self, idx: int, crop: dict) -> None:
        color = self._color(idx)
        ox1, oy1, ox2, oy2 = crop["rect"]
        cx1, cy1 = self._orig_to_canvas(ox1, oy1)
        cx2, cy2 = self._orig_to_canvas(ox2, oy2)
        rid = self.canvas.create_rectangle(cx1, cy1, cx2, cy2, outline=color, width=2)
        bbg = self.canvas.create_rectangle(cx1, cy1, cx1 + 26, cy1 + 26, fill=color, outline="")
        btx = self.canvas.create_text(cx1 + 13, cy1 + 13, text=str(idx + 1),
                                      fill=BG_DARK, font=("Segoe UI", 10, "bold"))
        crop["canvas_ids"] = (rid, bbg, btx)

    def _add_name_row(self, idx: int, name_var: tk.StringVar) -> None:
        color = self._color(idx)
        row = tk.Frame(self._list_inner, bg=BG_DARK, pady=4)
        row.pack(fill=tk.X, padx=2)
        tk.Label(row, text=f" {idx + 1} ", bg=color, fg=BG_DARK,
                 font=("Segoe UI", 10, "bold"), width=3).pack(side=tk.LEFT, padx=(0, 6))
        tk.Entry(row, textvariable=name_var, bg=BG_MID, fg=FG,
                 insertbackground=FG, relief=tk.FLAT,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT, fill=tk.X, expand=True)

    # ── Clear ─────────────────────────────────────────────────────────────────

    def _clear_all(self, keep_video: bool = False) -> None:
        for crop in self.crops:
            for cid in crop.get("canvas_ids", ()):
                self.canvas.delete(cid)
        self.crops.clear()
        for w in self._list_inner.winfo_children():
            w.destroy()
        self._progress.set(0)
        if not keep_video:
            self.video_path = ""
            self.orig_frame = None
            self.photo_img = None
            self.canvas.delete("all")
            self._draw_placeholder()
            self._vid_lbl.config(text="No video loaded")
            self._status.set("Open a video to begin.")
        else:
            self._status.set("Click and drag to draw crop regions.")

    # ── Processing ────────────────────────────────────────────────────────────

    def _start_crop_thread(self) -> None:
        if not self.video_path:
            messagebox.showwarning("No Video", "Please open a video file first.")
            return
        if not self.crops:
            messagebox.showwarning("No Crops", "Draw at least one crop region first.")
            return
        names = [c["name_var"].get().strip() for c in self.crops]
        if any(not n for n in names):
            messagebox.showwarning("Missing Name", "Enter a file name for every crop region.")
            return
        if len(set(names)) < len(names):
            messagebox.showwarning("Duplicate Names", "Each crop region must have a unique file name.")
            return
        rects = [c["rect"] for c in self.crops]
        out_dir = os.path.dirname(os.path.abspath(self.video_path))
        fmt_key = self._crop_fmt_var.get()
        self._progress.set(0)
        engine = f"ffmpeg — {fmt_key}" if _ffmpeg_available() else "OpenCV fallback"
        self._status.set(f"Cropping… [{engine}]")
        threading.Thread(target=self._do_crop,
                         args=(self.video_path, rects, names, out_dir, fmt_key),
                         daemon=True).start()

    def _do_crop(self, src, rects, names, out_dir, fmt_key) -> None:
        if _ffmpeg_available():
            self._do_crop_ffmpeg(src, rects, names, out_dir, fmt_key)
        else:
            src_ext = os.path.splitext(src)[1].lower()
            self._do_crop_opencv(src, rects, names, out_dir, src_ext)

    def _do_crop_ffmpeg(self, src, rects, names, out_dir, fmt_key) -> None:
        """One ffmpeg call per crop region using the chosen output format."""
        fmt_args, out_ext = OUTPUT_FORMATS.get(fmt_key, list(OUTPUT_FORMATS.values())[0])
        total = len(rects)
        for i, (rect, name) in enumerate(zip(rects, names)):
            x1, y1, x2, y2 = rect
            # Force even dimensions (required by H.264/H.265)
            w = (x2 - x1) & ~1
            h = (y2 - y1) & ~1
            out_path = os.path.join(out_dir, name + out_ext)
            try:
                _run_ffmpeg(
                    ["-hwaccel", "auto", "-i", src,
                     "-vf", f"yadif=0:-1:0,crop={w}:{h}:{x1}:{y1}"]
                    + fmt_args
                    + [out_path]
                )
            except RuntimeError as exc:
                self.root.after(0, messagebox.showerror, "ffmpeg error",
                                f"Region {i+1} failed:\n{exc}")
                return
            self.root.after(0, self._progress.set, (i + 1) / total * 100)
        self.root.after(0, self._on_crop_done, out_dir, total)

    def _do_crop_opencv(self, src, rects, names, out_dir, ext) -> None:
        """Fallback when ffmpeg is not available (uses OpenCV VideoWriter)."""
        cap = cv2.VideoCapture(src)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        fourcc = _fourcc(ext)
        writers = []
        for rect, name in zip(rects, names):
            x1, y1, x2, y2 = rect
            writer = cv2.VideoWriter(os.path.join(out_dir, name + ext),
                                     fourcc, fps, (x2 - x1, y2 - y1))
            writers.append((writer, x1, y1, x2, y2))
        fi = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            for writer, x1, y1, x2, y2 in writers:
                writer.write(frame[y1:y2, x1:x2])
            fi += 1
            self.root.after(0, self._progress.set, fi / total * 100)
        cap.release()
        for writer, *_ in writers:
            writer.release()
        self.root.after(0, self._on_crop_done, out_dir, len(names))

    def _on_crop_done(self, out_dir, count) -> None:
        self._progress.set(100)
        self._status.set(f"Done!  {count} file(s) saved to:\n{out_dir}")
        _play_cash_register()
        messagebox.showinfo("Crop Complete", f"{count} cropped video(s) saved to:\n{out_dir}")


# ════════════════════════════════════════════════════════════════════════════
# Clip Tab  (frame-accurate in/out trimmer)
# ════════════════════════════════════════════════════════════════════════════

class ClipTab:
    """
    Load any number of videos, scrub frame-by-frame, set in & out points,
    export each trimmed clip into a  'clipped'  sub-folder next to the source.
    """

    def __init__(self, parent: tk.Frame, root: tk.Tk) -> None:
        self.parent = parent
        self.root = root

        # Each entry: {path, total, fps, start_frame (None=0), end_frame (None=total-1)}
        self._files: list = []
        self._cur_idx: int = -1
        self._cur_frame: int = 0
        self._cap = None
        self._photo = None
        self._cur_raw = None
        self._scrub_lock = False
        self._split_running = False

        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Toolbar
        bar = tk.Frame(self.parent, bg=BG_DARK, pady=7)
        bar.pack(fill=tk.X)

        tk.Button(bar, text="  Add Videos  ", command=self._add_files,
                  bg=ACCENT, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=10)
        tk.Button(bar, text="  Remove Selected  ", command=self._remove_selected,
                  bg=RED, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=2)
        self._split_btn = tk.Button(
            bar,
            text="  Split Vertical  ",
            command=self._start_split_thread,
            bg=ACCENT,
            fg=BG_DARK,
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            padx=4,
        )
        self._split_btn.pack(side=tk.LEFT, padx=6)
        self._bar_lbl = tk.Label(
            bar,
            text="No files loaded",
            bg=BG_DARK,
            fg=FG_DIM,
            font=("Segoe UI", 10),
        )
        self._bar_lbl.pack(side=tk.LEFT, padx=10)

        # Body
        body = tk.Frame(self.parent, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # ── Left: file queue ──────────────────────────────────────────────────
        lf = tk.Frame(body, bg=BG_DARK, width=300)
        lf.pack(side=tk.LEFT, fill=tk.Y)
        lf.pack_propagate(False)

        tk.Label(lf, text="Video Queue", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(12, 6))

        lb_wrap = tk.Frame(lf, bg=BG_DARK)
        lb_wrap.pack(fill=tk.BOTH, expand=True, padx=6)
        lb_sb = tk.Scrollbar(lb_wrap, orient=tk.VERTICAL)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree = _build_queue_tree(lb_wrap, lb_sb)
        self._tree.bind("<<TreeviewSelect>>", self._on_file_select)

        self._file_status_lbl = tk.Label(lf, text="", bg=BG_DARK, fg=FG_DIM,
                                         font=("Segoe UI", 8), wraplength=280, justify=tk.LEFT)
        self._file_status_lbl.pack(padx=6, pady=4, anchor="w")

        # ── Center: preview + navigation ─────────────────────────────────────
        center = tk.Frame(body, bg=BG)
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8)

        cf = tk.Frame(center, bg=BG_MID, bd=1, relief=tk.SOLID)
        cf.pack(fill=tk.BOTH, expand=True)
        self._canvas = tk.Canvas(cf, bg=BG_DARK, highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.bind("<Configure>", lambda _: self._render_clip_frame())

        # Navigation row
        nav = tk.Frame(center, bg=BG, pady=5)
        nav.pack(fill=tk.X)
        btn = dict(bg=BG_MID, fg=FG, relief=tk.FLAT, font=("Segoe UI", 11), padx=6, pady=4)
        tk.Button(nav, text="◀◀", command=lambda: self._seek(-30), **btn).pack(side=tk.LEFT, padx=(0, 1))
        tk.Button(nav, text="◀",  command=lambda: self._seek(-1),  **btn).pack(side=tk.LEFT, padx=1)
        self._frame_lbl = tk.Label(nav, text="-- / --  (--:--:--;--)",
                                   bg=BG, fg=FG, font=("Segoe UI", 10), width=28)
        self._frame_lbl.pack(side=tk.LEFT, padx=8)
        tk.Button(nav, text="▶",  command=lambda: self._seek(1),   **btn).pack(side=tk.LEFT, padx=1)
        tk.Button(nav, text="▶▶", command=lambda: self._seek(30),  **btn).pack(side=tk.LEFT, padx=(1, 0))

        # Scrubber
        self._scrubber_var = tk.IntVar(value=0)
        self._scrubber = tk.Scale(
            center, orient=tk.HORIZONTAL, from_=0, to=1,
            variable=self._scrubber_var, command=self._on_scrub,
            bg=BG, fg=FG, troughcolor=BG_MID, highlightthickness=0,
            showvalue=False, sliderlength=14, bd=0,
        )
        self._scrubber.pack(fill=tk.X, padx=2, pady=(0, 2))

        # Marker overlay (thin canvas under the scrubber)
        self._marker_canvas = tk.Canvas(center, bg=BG, height=14, highlightthickness=0)
        self._marker_canvas.pack(fill=tk.X, padx=2)

        # ── Right: controls ───────────────────────────────────────────────────
        rf = tk.Frame(body, bg=BG_DARK, width=265)
        rf.pack(side=tk.RIGHT, fill=tk.Y)
        rf.pack_propagate(False)

        tk.Label(rf, text="Clip Controls", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 12, "bold")).pack(pady=(14, 4))

        self._clip_file_lbl = tk.Label(rf, text="— no file selected —",
                                       bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9), wraplength=240)
        self._clip_file_lbl.pack(padx=8, pady=(0, 6))

        # Set Start
        _separator(rf)
        sr = tk.Frame(rf, bg=BG_DARK, pady=6)
        sr.pack(fill=tk.X, padx=10)
        tk.Button(sr, text="Set Start  [I]", command=self._set_start,
                  bg=PURPLE, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT)
        self._start_lbl = tk.Label(sr, text="(beginning)", bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9))
        self._start_lbl.pack(side=tk.LEFT, padx=8)

        # Set End
        er = tk.Frame(rf, bg=BG_DARK, pady=4)
        er.pack(fill=tk.X, padx=10)
        tk.Button(er, text=" Set End  [O]", command=self._set_end,
                  bg=YELLOW, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT)
        self._end_lbl = tk.Label(er, text="(end)", bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9))
        self._end_lbl.pack(side=tk.LEFT, padx=8)

        tk.Button(rf, text="Clear Points for This File", command=self._clear_points,
                  bg=BG_MID, fg=FG_DIM, font=("Segoe UI", 9),
                  relief=tk.FLAT, pady=3).pack(padx=10, pady=(6, 2), fill=tk.X)

        # Queue summary
        _separator(rf)
        tk.Label(rf, text="Clip Queue Summary", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(pady=(8, 4))
        q_wrap = tk.Frame(rf, bg=BG_DARK)
        q_wrap.pack(fill=tk.BOTH, expand=True, padx=8)
        q_sb = tk.Scrollbar(q_wrap, orient=tk.VERTICAL)
        q_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._queue_text = tk.Text(q_wrap, yscrollcommand=q_sb.set,
                                   bg=BG_MID, fg=FG, font=("Segoe UI", 8),
                                   relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD,
                                   highlightthickness=0, borderwidth=0)
        self._queue_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        q_sb.config(command=self._queue_text.yview)

        # Format selector
        _separator(rf)
        cfmt_row = tk.Frame(rf, bg=BG_DARK)
        cfmt_row.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(cfmt_row, text="Output Format:", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._clip_fmt_var = tk.StringVar(value=CLIP_FORMAT_NAMES[0])
        ttk.Combobox(cfmt_row, textvariable=self._clip_fmt_var,
                     values=CLIP_FORMAT_NAMES, state="readonly",
                     font=("Segoe UI", 9)).pack(fill=tk.X, pady=(2, 0))

        self._norm_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            rf, text="Normalize brightness", variable=self._norm_var,
            bg=BG_DARK, fg=FG, selectcolor=BG_MID,
            activebackground=BG_DARK, activeforeground=FG,
            font=("Segoe UI", 9),
        ).pack(padx=12, anchor="w", pady=(6, 0))

        # Progress + CLIP ALL
        _separator(rf)
        self._clip_progress = tk.DoubleVar(value=0)
        ttk.Progressbar(rf, variable=self._clip_progress, maximum=100,
                        style="Clip.Horizontal.TProgressbar", length=245
                        ).pack(padx=10, pady=(8, 3))
        tk.Button(rf, text="  CLIP ALL", command=self._start_clip_thread,
                  bg=GREEN, fg=BG_DARK, font=("Segoe UI", 13, "bold"),
                  relief=tk.FLAT, pady=11).pack(fill=tk.X, padx=10, pady=(3, 4))
        self._clip_status = tk.StringVar(value="Add video files to begin.")
        tk.Label(rf, textvariable=self._clip_status, bg=BG_DARK, fg=FG_DIM,
                 font=("Segoe UI", 9), wraplength=245, justify=tk.LEFT
                 ).pack(padx=10, anchor="w", pady=(0, 8))

        # ── Keyboard shortcuts ─────────────────────────────────────────────────
        self.root.bind("<Left>",  lambda e: self._seek(-1))
        self.root.bind("<Right>", lambda e: self._seek(1))
        self.root.bind("<Up>",    lambda e: self._nav_file(-1))
        self.root.bind("<Down>",  lambda e: self._nav_file(1))
        self.root.bind("i",       lambda e: self._set_start())
        self.root.bind("o",       lambda e: self._set_end())
        # Override default widget arrow-key behaviour to prevent double-action
        self._scrubber.bind("<Left>",  lambda e: (self._seek(-1),     "break")[-1])
        self._scrubber.bind("<Right>", lambda e: (self._seek(1),      "break")[-1])
        self._scrubber.bind("<Up>",    lambda e: (self._nav_file(-1), "break")[-1])
        self._scrubber.bind("<Down>",  lambda e: (self._nav_file(1),  "break")[-1])
        self._tree.bind("<Up>",        lambda e: (self._nav_file(-1), "break")[-1])
        self._tree.bind("<Down>",      lambda e: (self._nav_file(1),  "break")[-1])

        self.root.after(150, self._draw_placeholder)

    # ── Placeholder ───────────────────────────────────────────────────────────

    def _draw_placeholder(self) -> None:
        self._canvas.delete("placeholder")
        w, h = self._canvas.winfo_width(), self._canvas.winfo_height()
        self._canvas.create_text(w // 2, h // 2,
                                 text="Add videos and select one to preview",
                                 fill="#45475a", font=("Segoe UI", 16), tags="placeholder")

    # ── File management ───────────────────────────────────────────────────────

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Select video files", filetypes=VIDEO_FILETYPES)
        if not paths:
            return
        existing = {f["path"] for f in self._files}
        added = 0
        for p in paths:
            if p in existing:
                continue
            cap = cv2.VideoCapture(p)
            if not cap.isOpened():
                cap.release()
                continue
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            cap.release()
            if total <= 0:
                continue
            self._files.append({"path": p, "total": total, "fps": fps,
                                 "start_frame": None, "end_frame": None})
            added += 1
        if added:
            self._refresh_list()
            self._bar_lbl.config(text=f"{len(self._files)} file(s) loaded")
            self._update_queue_summary()
            self._clip_status.set(f"Added {added} file(s). Select one to preview.")
            if self._cur_idx == -1 and self._files:
                self._tree.selection_set("0")
                self._tree.focus("0")
                self._load_file(0)

    def _refresh_list(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for i, f in enumerate(self._files):
            self._tree.insert("", "end", iid=str(i),
                              text=os.path.basename(f["path"]),
                              values=(_folder_of(f["path"]),))
        if self._cur_idx >= 0:
            self._tree.selection_set(str(self._cur_idx))
            self._tree.focus(str(self._cur_idx))

    def _remove_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        cur_path = self._files[self._cur_idx]["path"] if self._cur_idx >= 0 else None
        for idx in sorted((int(i) for i in sel), reverse=True):
            self._files.pop(idx)
        new_cur = next((i for i, f in enumerate(self._files)
                        if f["path"] == cur_path), -1) if cur_path else -1
        if new_cur < 0:
            self._release_cap()
            self._cur_idx = -1
            self._cur_raw = None
            self._canvas.delete("all")
            self._draw_placeholder()
            self._frame_lbl.config(text="-- / --  (--:--:--;--)")
            self._scrubber.config(to=1)
            self._scrubber_var.set(0)
            self._clip_file_lbl.config(text="— no file selected —")
            self._start_lbl.config(text="(beginning)")
            self._end_lbl.config(text="(end)")
            self._file_status_lbl.config(text="")
        else:
            self._cur_idx = new_cur
        self._refresh_list()
        self._bar_lbl.config(text=f"{len(self._files)} file(s) loaded" if self._files else "No files loaded")
        self._update_queue_summary()

    # ── File selection ────────────────────────────────────────────────────────

    def _on_file_select(self, _=None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx == self._cur_idx:
            return
        self._load_file(idx)

    def _load_file(self, idx: int) -> None:
        self._release_cap()
        self._cur_idx = idx
        entry = self._files[idx]
        self._cap = cv2.VideoCapture(entry["path"])
        self._cur_frame = 0
        self._scrub_lock = True
        self._scrubber.config(to=max(1, entry["total"] - 1))
        self._scrubber_var.set(0)
        self._scrub_lock = False
        self._clip_file_lbl.config(text=os.path.basename(entry["path"]))
        self._refresh_point_labels()
        self._seek_to(0)
        self._update_file_status_label()

    def _release_cap(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _nav_file(self, delta: int) -> None:
        """Select the previous (-1) or next (+1) file in the queue."""
        if not self._files:
            return
        if self._cur_idx < 0:
            new_idx = 0
        else:
            new_idx = max(0, min(self._cur_idx + delta, len(self._files) - 1))
        if new_idx == self._cur_idx:
            return
        self._tree.selection_set(str(new_idx))
        self._tree.focus(str(new_idx))
        self._tree.see(str(new_idx))
        self._load_file(new_idx)

    # ── Frame navigation ──────────────────────────────────────────────────────

    def _seek(self, delta: int) -> None:
        if self._cur_idx < 0 or self._cap is None:
            return
        total = self._files[self._cur_idx]["total"]
        self._seek_to(max(0, min(self._cur_frame + delta, total - 1)))

    def _seek_to(self, frame_idx: int) -> None:
        if self._cap is None:
            return
        self._cur_frame = frame_idx
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, raw = self._cap.read()
        if not ok:
            return
        self._cur_raw = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
        self._render_clip_frame()
        entry = self._files[self._cur_idx]
        tc = _frames_to_tc(frame_idx, entry["fps"])
        self._frame_lbl.config(text=f"Frame {frame_idx + 1} / {entry['total']}  ({tc})")
        self._scrub_lock = True
        self._scrubber_var.set(frame_idx)
        self._scrub_lock = False
        self._draw_marker_overlay()

    def _on_scrub(self, val) -> None:
        if self._scrub_lock or self._cur_idx < 0:
            return
        self._seek_to(int(float(val)))

    def _render_clip_frame(self) -> None:
        if self._cur_raw is None:
            self._draw_placeholder()
            return
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        if cw < 4 or ch < 4:
            self.root.after(60, self._render_clip_frame)
            return
        oh, ow = self._cur_raw.shape[:2]
        ratio = min(cw / ow, ch / oh)
        nw, nh = int(ow * ratio), int(oh * ratio)
        off_x, off_y = (cw - nw) // 2, (ch - nh) // 2
        img = Image.fromarray(self._cur_raw).resize((nw, nh), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        self._canvas.delete("all")
        self._canvas.create_image(off_x, off_y, anchor=tk.NW, image=self._photo)

    # ── Marker overlay ────────────────────────────────────────────────────────

    def _draw_marker_overlay(self) -> None:
        if self._cur_idx < 0:
            return
        entry = self._files[self._cur_idx]
        total = max(1, entry["total"] - 1)
        mc = self._marker_canvas
        mc.delete("all")
        w = mc.winfo_width()
        if w < 4:
            return
        sf = entry["start_frame"]
        ef = entry["end_frame"]
        sx = int((sf if sf is not None else 0) / total * w)
        ex = int((ef if ef is not None else total) / total * w)
        mc.create_rectangle(sx, 0, ex, 14, fill=BG_MID, outline="")
        if sf is not None:
            x = int(sf / total * w)
            mc.create_line(x, 0, x, 14, fill=PURPLE, width=2)
            mc.create_text(x + 3, 7, text="▶", fill=PURPLE, font=("Segoe UI", 7), anchor="w")
        if ef is not None:
            x = int(ef / total * w)
            mc.create_line(x, 0, x, 14, fill=YELLOW, width=2)
            mc.create_text(x - 3, 7, text="◀", fill=YELLOW, font=("Segoe UI", 7), anchor="e")

    # ── In / Out controls ─────────────────────────────────────────────────────

    def _set_start(self) -> None:
        if self._cur_idx < 0:
            return
        entry = self._files[self._cur_idx]
        ef = entry["end_frame"]
        if ef is not None and self._cur_frame >= ef:
            messagebox.showwarning("Invalid Point", "Start frame must be before end frame.")
            return
        entry["start_frame"] = self._cur_frame
        self._refresh_point_labels()
        self._draw_marker_overlay()
        self._update_queue_summary()
        self._update_file_status_label()

    def _set_end(self) -> None:
        if self._cur_idx < 0:
            return
        entry = self._files[self._cur_idx]
        sf = entry["start_frame"]
        if sf is not None and self._cur_frame <= sf:
            messagebox.showwarning("Invalid Point", "End frame must be after start frame.")
            return
        entry["end_frame"] = self._cur_frame
        self._refresh_point_labels()
        self._draw_marker_overlay()
        self._update_queue_summary()
        self._update_file_status_label()

    def _clear_points(self) -> None:
        if self._cur_idx < 0:
            return
        entry = self._files[self._cur_idx]
        entry["start_frame"] = None
        entry["end_frame"] = None
        self._refresh_point_labels()
        self._draw_marker_overlay()
        self._update_queue_summary()
        self._update_file_status_label()

    def _refresh_point_labels(self) -> None:
        if self._cur_idx < 0:
            self._start_lbl.config(text="(beginning)")
            self._end_lbl.config(text="(end)")
            return
        entry = self._files[self._cur_idx]
        fps = entry["fps"]
        sf, ef = entry["start_frame"], entry["end_frame"]
        self._start_lbl.config(
            text=f"Frame {sf}  ({_frames_to_tc(sf, fps)})" if sf is not None else "(beginning)")
        self._end_lbl.config(
            text=f"Frame {ef}  ({_frames_to_tc(ef, fps)})" if ef is not None else "(end)")

    def _update_file_status_label(self) -> None:
        if self._cur_idx < 0:
            self._file_status_lbl.config(text="")
            return
        entry = self._files[self._cur_idx]
        fps = entry["fps"]
        sf = entry["start_frame"] if entry["start_frame"] is not None else 0
        ef = entry["end_frame"] if entry["end_frame"] is not None else entry["total"] - 1
        dur = (ef - sf) / max(fps, 0.001)
        self._file_status_lbl.config(
            text=f"In:  {_frames_to_tc(sf, fps)}\nOut: {_frames_to_tc(ef, fps)}\nDur: {dur:.2f}s")

    # ── Queue summary ─────────────────────────────────────────────────────────

    def _update_queue_summary(self) -> None:
        self._queue_text.config(state=tk.NORMAL)
        self._queue_text.delete("1.0", tk.END)
        for i, entry in enumerate(self._files, 1):
            fps = entry["fps"]
            sf = entry["start_frame"]
            ef = entry["end_frame"]
            sf_tc = _frames_to_tc(sf, fps) if sf is not None else "00:00:00;00"
            ef_tc = _frames_to_tc(ef if ef is not None else entry["total"] - 1, fps)
            self._queue_text.insert(
                tk.END, f"{i}. {os.path.basename(entry['path'])}\n  ▶ {sf_tc} → {ef_tc}\n\n")
        self._queue_text.config(state=tk.DISABLED)

    # ── Quick Splitter (vertical) ──────────────────────────────────────────
    def _start_split_thread(self) -> None:
        if self._split_running:
            return
        if not self._files:
            messagebox.showwarning("No Files", "Add at least one video file first.")
            return
        self._split_running = True
        self._split_btn.config(state=tk.DISABLED)
        self._clip_progress.set(0)
        fmt_key = self._clip_fmt_var.get()
        engine = f"ffmpeg — {fmt_key}" if _ffmpeg_available() else "OpenCV fallback"
        self._clip_status.set(f"Splitting… [{engine}]")
        self._bar_lbl.config(text=f"Splitting 0/{len(self._files)}…")
        paths = [f["path"] for f in self._files]
        threading.Thread(target=self._do_split, args=(paths, fmt_key), daemon=True).start()

    def _do_split(self, paths: list, fmt_key: str = "") -> None:
        try:
            if _ffmpeg_available():
                self._do_split_ffmpeg(paths, fmt_key)
            else:
                self._do_split_opencv(paths)
        except Exception as exc:
            self.root.after(0, messagebox.showerror, "Split Error",
                            f"Unexpected split failure:\n{exc}")
            self.root.after(0, self._on_split_failed, "Split failed. See error details.")

    def _do_split_ffmpeg(self, paths: list, fmt_key: str) -> None:
        fmt_args, out_ext = CLIP_FORMATS.get(fmt_key, list(CLIP_FORMATS.values())[0])
        # If user selected a stream-copy format (no re-encode), ffmpeg cannot
        # apply filters like crop. Detect '-c copy' and fall back to a
        # re-encode output format to allow filtering.
        if any(arg == "copy" for arg in fmt_args):
            # Pick a safe re-encode format (first entry in OUTPUT_FORMATS)
            fmt_args, out_ext = list(OUTPUT_FORMATS.values())[0]
        total = len(paths)
        normalize = self._norm_var.get()
        luma_map: dict = {}
        norm_target = 128.0
        if normalize:
            self.root.after(0, self._clip_status.set, "Measuring brightness…")
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, total)) as exe:
                futures = {exe.submit(_measure_luma, src): src for src in paths}
                for fut in concurrent.futures.as_completed(futures):
                    luma_map[futures[fut]] = fut.result()
            measured = list(luma_map.values())
            norm_target = sum(measured) / len(measured)
        for idx, src in enumerate(paths, 1):
            self.root.after(0, self._clip_status.set,
                            f"Splitting {idx}/{total}: {os.path.basename(src)}")
            self.root.after(0, self._bar_lbl.config, {"text": f"Splitting {idx}/{total}…"})
            src_dir = os.path.dirname(os.path.abspath(src))
            out_dir = os.path.join(src_dir, "split")
            os.makedirs(out_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(src))[0]
            left_path = os.path.join(out_dir, base + "_left" + out_ext)
            right_path = os.path.join(out_dir, base + "_right" + out_ext)
            norm_suffix = ""
            if normalize:
                luma = max(luma_map.get(src, 128.0), 1.0)
                factor = max(0.05, min(norm_target / luma, 20.0))
                norm_suffix = f",format=yuv420p,lutyuv=y='clip(val*{factor:.6f},0,255)'"
            try:
                # left half
                _run_ffmpeg(["-hwaccel", "auto", "-i", src,
                             "-vf", f"yadif=0:-1:0,crop=iw/2:ih:0:0{norm_suffix}"] + fmt_args + [left_path])
                # right half
                _run_ffmpeg(["-hwaccel", "auto", "-i", src,
                             "-vf", f"yadif=0:-1:0,crop=iw/2:ih:iw/2:0{norm_suffix}"] + fmt_args + [right_path])
            except RuntimeError as exc:
                self.root.after(0, messagebox.showerror, "ffmpeg error",
                                f"{os.path.basename(src)} failed:\n{exc}")
                self.root.after(0, self._on_split_failed,
                                f"Split failed on {os.path.basename(src)}.")
                return
            self.root.after(0, self._clip_progress.set, idx / total * 100)
        self.root.after(0, self._on_split_done, total)

    def _do_split_opencv(self, paths: list) -> None:
        total = len(paths)
        for idx, src in enumerate(paths, 1):
            self.root.after(0, self._clip_status.set,
                            f"Splitting {idx}/{total}: {os.path.basename(src)}")
            self.root.after(0, self._bar_lbl.config, {"text": f"Splitting {idx}/{total}…"})
            cap = cv2.VideoCapture(src)
            if not cap.isOpened():
                continue
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            half_w = w // 2
            ext = os.path.splitext(src)[1].lower()
            base = os.path.splitext(os.path.basename(src))[0]
            out_dir = os.path.join(os.path.dirname(src), "split")
            os.makedirs(out_dir, exist_ok=True)
            left_out = os.path.join(out_dir, base + "_left" + ext)
            right_out = os.path.join(out_dir, base + "_right" + ext)
            fourcc = _fourcc(ext)
            writer_l = cv2.VideoWriter(left_out, fourcc, fps, (half_w, h))
            writer_r = cv2.VideoWriter(right_out, fourcc, fps, (half_w, h))
            if not writer_l.isOpened() or not writer_r.isOpened():
                cap.release()
                writer_l.release()
                writer_r.release()
                self.root.after(
                    0,
                    messagebox.showerror,
                    "Split Error",
                    f"Could not create output writers for:\n{os.path.basename(src)}\n"
                    "Try installing ffmpeg and run split again.",
                )
                self.root.after(0, self._on_split_failed,
                                f"Split failed on {os.path.basename(src)}.")
                return
            for _f in range(total_frames):
                ok, frame = cap.read()
                if not ok:
                    break
                l = frame[:, :half_w]
                r = frame[:, w - half_w:]
                writer_l.write(l)
                writer_r.write(r)
                # Update occasionally to keep UI responsive during long files.
                if _f % 120 == 0:
                    overall = ((idx - 1) + (_f + 1) / max(1, total_frames)) / total * 100
                    self.root.after(0, self._clip_progress.set, overall)
            cap.release()
            writer_l.release()
            writer_r.release()
            self.root.after(0, self._clip_progress.set, idx / total * 100)
        self.root.after(0, self._on_split_done, total)

    def _on_split_done(self, count: int) -> None:
        self._split_running = False
        self._split_btn.config(state=tk.NORMAL)
        self._clip_progress.set(100)
        self._clip_status.set(f"Done!  {count} split(s) saved next to their source files.")
        self._bar_lbl.config(text=f"{len(self._files)} file(s) loaded")
        _play_cash_register()
        messagebox.showinfo("Split Complete", f"{count} file(s) processed.\nLeft/right files saved next to each source.")

    def _on_split_failed(self, status_text: str) -> None:
        self._split_running = False
        self._split_btn.config(state=tk.NORMAL)
        self._clip_status.set(status_text)
        self._bar_lbl.config(text=f"{len(self._files)} file(s) loaded")

    # ── Clip processing ───────────────────────────────────────────────────────

    def _start_clip_thread(self) -> None:
        if not self._files:
            messagebox.showwarning("No Files", "Add at least one video file first.")
            return
        self._clip_progress.set(0)
        fmt_key = self._clip_fmt_var.get()
        engine = f"ffmpeg — {fmt_key}" if _ffmpeg_available() else "OpenCV fallback"
        self._clip_status.set(f"Clipping… [{engine}]")
        jobs = [
            (f["path"],
             f["start_frame"] if f["start_frame"] is not None else 0,
             f["end_frame"] if f["end_frame"] is not None else f["total"] - 1,
             f["fps"])
            for f in self._files
        ]
        threading.Thread(target=self._do_clip, args=(jobs, fmt_key), daemon=True).start()

    def _do_clip(self, jobs: list, fmt_key: str = "") -> None:
        if _ffmpeg_available():
            self._do_clip_ffmpeg(jobs, fmt_key)
        else:
            self._do_clip_opencv(jobs)

    def _do_clip_ffmpeg(self, jobs: list, fmt_key: str) -> None:
        """Trim with ffmpeg using the chosen output format."""
        fmt_args, out_ext = CLIP_FORMATS.get(fmt_key, list(CLIP_FORMATS.values())[0])
        total_jobs = len(jobs)
        normalize = self._norm_var.get()
        is_copy = "-c" in fmt_args and "copy" in fmt_args
        # Pre-measure brightness in parallel when normalisation is requested.
        # If stream-copy was selected we must re-encode to apply filters.
        luma_map: dict = {}
        norm_target = 128.0
        if normalize:
            if is_copy:
                fmt_args, out_ext = list(OUTPUT_FORMATS.values())[0]
                is_copy = False
            self.root.after(0, self._clip_status.set, "Measuring brightness\u2026")
            paths = [src for src, *_ in jobs]
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(paths))) as exe:
                futures = {exe.submit(_measure_luma, src): src for src in paths}
                for fut in concurrent.futures.as_completed(futures):
                    luma_map[futures[fut]] = fut.result()
            measured = list(luma_map.values())
            norm_target = sum(measured) / len(measured)
        for job_n, (src, start_f, end_f, fps) in enumerate(jobs, 1):
            src_dir = os.path.dirname(os.path.abspath(src))
            out_dir = os.path.join(src_dir, "clipped")
            os.makedirs(out_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(src))[0]
            out_path = os.path.join(out_dir, base + out_ext)
            cap_fps = fps if fps > 0 else 30.0
            start_ts = start_f / cap_fps
            end_ts   = end_f   / cap_fps
            try:
                if is_copy:
                    _run_ffmpeg(
                        ["-ss", f"{start_ts:.6f}",
                         "-i", src,
                         "-t", f"{end_ts - start_ts:.6f}"]
                        + fmt_args
                        + [out_path]
                    )
                else:
                    vf = "yadif=0:-1:0"
                    if normalize:
                        luma = max(luma_map.get(src, 128.0), 1.0)
                        factor = max(0.05, min(norm_target / luma, 20.0))
                        vf += f",format=yuv420p,lutyuv=y='clip(val*{factor:.6f},0,255)'"
                    _run_ffmpeg(
                        ["-hwaccel", "auto",
                         "-ss", f"{start_ts:.6f}",
                         "-i", src,
                         "-t", f"{end_ts - start_ts:.6f}",
                         "-vf", vf]
                        + fmt_args
                        + [out_path]
                    )
            except RuntimeError as exc:
                self.root.after(0, messagebox.showerror, "ffmpeg error",
                                f"{os.path.basename(src)} failed:\n{exc}")
                return
            self.root.after(0, self._clip_progress.set, job_n / total_jobs * 100)
        self.root.after(0, self._on_clip_done, total_jobs)

    def _do_clip_opencv(self, jobs: list) -> None:
        """Fallback when ffmpeg is not available."""
        total_jobs = len(jobs)
        for job_n, (src, start_f, end_f, fps) in enumerate(jobs, 1):
            src_dir = os.path.dirname(os.path.abspath(src))
            out_dir = os.path.join(src_dir, "clipped")
            os.makedirs(out_dir, exist_ok=True)
            ext = os.path.splitext(src)[1].lower()
            out_path = os.path.join(out_dir, os.path.basename(src))
            cap = cv2.VideoCapture(src)
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap_fps = cap.get(cv2.CAP_PROP_FPS) or fps
            writer = cv2.VideoWriter(out_path, _fourcc(ext), cap_fps, (w, h))
            cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)
            clip_total = max(1, end_f - start_f + 1)
            for fi in range(clip_total):
                ok, frame = cap.read()
                if not ok:
                    break
                writer.write(frame)
                overall = (job_n - 1 + (fi + 1) / clip_total) / total_jobs * 100
                self.root.after(0, self._clip_progress.set, overall)
            cap.release()
            writer.release()
        self.root.after(0, self._on_clip_done, total_jobs)

    def _on_clip_done(self, count: int) -> None:
        self._clip_progress.set(100)
        self._clip_status.set(
            f"Done!  {count} clip(s) saved to their respective 'clipped' folders.")
        _play_cash_register()
        messagebox.showinfo(
            "Clip Complete",
            f"{count} clip(s) exported.\n"
            "Each file is in a 'clipped' folder next to its source video.")


# ════════════════════════════════════════════════════════════════════════════
# Cut & Stitch Tab  (mark ranges to REMOVE, stitch the survivors together)
# ════════════════════════════════════════════════════════════════════════════

class CutStitchTab:
    """
    Load a single video, scrub frame-by-frame, and mark one or more ranges to
    cut OUT. On export the surviving segments are stitched back together into a
    single video saved in a 'cut' sub-folder next to the source.

    This is the inverse of the Trim/Keep tab: there you pick the one span to
    keep, here you pick the spans to discard.
    """

    def __init__(self, parent: tk.Frame, root: tk.Tk) -> None:
        self.parent = parent
        self.root = root

        self._path = None
        self._total = 0
        self._fps = 30.0
        self._cap = None
        self._photo = None
        self._cur_raw = None
        self._cur_frame = 0
        self._scrub_lock = False
        self._running = False

        # Committed cut ranges: list of (start_frame, end_frame) inclusive.
        self._cuts: list = []
        # Pending in/out before the range is added to the list.
        self._pending_start = None
        self._pending_end = None

        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Toolbar
        bar = tk.Frame(self.parent, bg=BG_DARK, pady=7)
        bar.pack(fill=tk.X)
        tk.Button(bar, text="  Load Video  ", command=self._load_video,
                  bg=ACCENT, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=10)
        self._bar_lbl = tk.Label(bar, text="No file loaded", bg=BG_DARK, fg=FG_DIM,
                                 font=("Segoe UI", 10))
        self._bar_lbl.pack(side=tk.LEFT, padx=10)

        # Body
        body = tk.Frame(self.parent, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # ── Center: preview + navigation ─────────────────────────────────────
        center = tk.Frame(body, bg=BG)
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8)

        cf = tk.Frame(center, bg=BG_MID, bd=1, relief=tk.SOLID)
        cf.pack(fill=tk.BOTH, expand=True)
        self._canvas = tk.Canvas(cf, bg=BG_DARK, highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.bind("<Configure>", lambda _: self._render_frame())

        nav = tk.Frame(center, bg=BG, pady=5)
        nav.pack(fill=tk.X)
        btn = dict(bg=BG_MID, fg=FG, relief=tk.FLAT, font=("Segoe UI", 11), padx=6, pady=4)
        tk.Button(nav, text="◀◀", command=lambda: self._seek(-30), **btn).pack(side=tk.LEFT, padx=(0, 1))
        tk.Button(nav, text="◀",  command=lambda: self._seek(-1),  **btn).pack(side=tk.LEFT, padx=1)
        self._frame_lbl = tk.Label(nav, text="-- / --  (--:--:--;--)",
                                   bg=BG, fg=FG, font=("Segoe UI", 10), width=28)
        self._frame_lbl.pack(side=tk.LEFT, padx=8)
        tk.Button(nav, text="▶",  command=lambda: self._seek(1),   **btn).pack(side=tk.LEFT, padx=1)
        tk.Button(nav, text="▶▶", command=lambda: self._seek(30),  **btn).pack(side=tk.LEFT, padx=(1, 0))

        # Scrubber
        self._scrubber_var = tk.IntVar(value=0)
        self._scrubber = tk.Scale(
            center, orient=tk.HORIZONTAL, from_=0, to=1,
            variable=self._scrubber_var, command=self._on_scrub,
            bg=BG, fg=FG, troughcolor=BG_MID, highlightthickness=0,
            showvalue=False, sliderlength=14, bd=0,
        )
        self._scrubber.pack(fill=tk.X, padx=2, pady=(0, 2))

        # Marker overlay (shows the cut ranges in red along the timeline)
        self._marker_canvas = tk.Canvas(center, bg=BG, height=14, highlightthickness=0)
        self._marker_canvas.pack(fill=tk.X, padx=2)

        # Local arrow-key navigation (bound to our own widgets with "break" so
        # the Trim tab's global root bindings don't also fire).
        for w in (self._canvas, self._scrubber, self._marker_canvas):
            w.bind("<Left>",  lambda e: (self._seek(-1),  "break")[-1])
            w.bind("<Right>", lambda e: (self._seek(1),   "break")[-1])
            w.bind("<Prior>", lambda e: (self._seek(-30), "break")[-1])
            w.bind("<Next>",  lambda e: (self._seek(30),  "break")[-1])

        # ── Right: controls ───────────────────────────────────────────────────
        rf = tk.Frame(body, bg=BG_DARK, width=275)
        rf.pack(side=tk.RIGHT, fill=tk.Y)
        rf.pack_propagate(False)

        tk.Label(rf, text="Cut & Stitch", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 12, "bold")).pack(pady=(14, 2))
        tk.Label(rf, text="Mark ranges to remove; the rest is stitched together.",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 8),
                 wraplength=250, justify=tk.CENTER).pack(padx=8, pady=(0, 4))

        self._file_lbl = tk.Label(rf, text="— no file loaded —",
                                  bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9), wraplength=250)
        self._file_lbl.pack(padx=8, pady=(0, 4))

        # Pending cut start / end
        _separator(rf)
        sr = tk.Frame(rf, bg=BG_DARK, pady=6)
        sr.pack(fill=tk.X, padx=10)
        tk.Button(sr, text="Cut Start", command=self._set_cut_start,
                  bg=PURPLE, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT)
        self._cut_start_lbl = tk.Label(sr, text="(none)", bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9))
        self._cut_start_lbl.pack(side=tk.LEFT, padx=8)

        er = tk.Frame(rf, bg=BG_DARK, pady=4)
        er.pack(fill=tk.X, padx=10)
        tk.Button(er, text="Cut End ", command=self._set_cut_end,
                  bg=YELLOW, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT)
        self._cut_end_lbl = tk.Label(er, text="(none)", bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9))
        self._cut_end_lbl.pack(side=tk.LEFT, padx=8)

        tk.Button(rf, text="＋ Add Cut Range", command=self._add_cut,
                  bg=RED, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, pady=4).pack(padx=10, pady=(6, 2), fill=tk.X)

        # List of committed cut ranges
        _separator(rf)
        tk.Label(rf, text="Cut Ranges", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(pady=(6, 4))
        lb_wrap = tk.Frame(rf, bg=BG_DARK)
        lb_wrap.pack(fill=tk.BOTH, expand=True, padx=10)
        lb_sb = tk.Scrollbar(lb_wrap, orient=tk.VERTICAL)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cut_list = tk.Listbox(
            lb_wrap, yscrollcommand=lb_sb.set, bg=BG_MID, fg=FG,
            font=("Segoe UI", 8), relief=tk.FLAT, highlightthickness=0,
            selectbackground=ACCENT, selectforeground=BG_DARK, activestyle="none")
        self._cut_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lb_sb.config(command=self._cut_list.yview)

        rm_row = tk.Frame(rf, bg=BG_DARK)
        rm_row.pack(fill=tk.X, padx=10, pady=(4, 2))
        tk.Button(rm_row, text="Remove", command=self._remove_cut,
                  bg=BG_MID, fg=FG_DIM, font=("Segoe UI", 9),
                  relief=tk.FLAT, pady=3).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))
        tk.Button(rm_row, text="Clear All", command=self._clear_cuts,
                  bg=BG_MID, fg=FG_DIM, font=("Segoe UI", 9),
                  relief=tk.FLAT, pady=3).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))

        # Format + export
        _separator(rf)
        fmt_row = tk.Frame(rf, bg=BG_DARK)
        fmt_row.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(fmt_row, text="Output Format:", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._fmt_var = tk.StringVar(value=CLIP_FORMAT_NAMES[0])
        ttk.Combobox(fmt_row, textvariable=self._fmt_var,
                     values=CLIP_FORMAT_NAMES, state="readonly",
                     font=("Segoe UI", 9)).pack(fill=tk.X, pady=(2, 0))

        _separator(rf)
        self._progress = tk.DoubleVar(value=0)
        ttk.Progressbar(rf, variable=self._progress, maximum=100,
                        style="Clip.Horizontal.TProgressbar", length=255
                        ).pack(padx=10, pady=(8, 3))
        self._export_btn = tk.Button(
            rf, text="  EXPORT STITCHED", command=self._start_export,
            bg=GREEN, fg=BG_DARK, font=("Segoe UI", 12, "bold"),
            relief=tk.FLAT, pady=10)
        self._export_btn.pack(fill=tk.X, padx=10, pady=(3, 4))
        self._status = tk.StringVar(value="Load a video to begin.")
        tk.Label(rf, textvariable=self._status, bg=BG_DARK, fg=FG_DIM,
                 font=("Segoe UI", 9), wraplength=255, justify=tk.LEFT
                 ).pack(padx=10, anchor="w", pady=(0, 8))

        self.root.after(150, self._draw_placeholder)

    # ── Placeholder ───────────────────────────────────────────────────────────

    def _draw_placeholder(self) -> None:
        self._canvas.delete("placeholder")
        w, h = self._canvas.winfo_width(), self._canvas.winfo_height()
        self._canvas.create_text(w // 2, h // 2,
                                 text="Load a video to mark cut ranges",
                                 fill="#45475a", font=("Segoe UI", 16), tags="placeholder")

    # ── File management ────────────────────────────────────────────────────────

    def _load_video(self) -> None:
        path = filedialog.askopenfilename(title="Select a video file", filetypes=VIDEO_FILETYPES)
        if not path:
            return
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            cap.release()
            messagebox.showerror("Load Error", "Could not open that video file.")
            return
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.release()
        if total <= 0:
            messagebox.showerror("Load Error", "That video reports zero frames.")
            return
        self._release_cap()
        self._path = path
        self._total = total
        self._fps = fps
        self._cap = cv2.VideoCapture(path)
        self._cur_frame = 0
        self._cuts = []
        self._pending_start = None
        self._pending_end = None
        self._scrub_lock = True
        self._scrubber.config(to=max(1, total - 1))
        self._scrubber_var.set(0)
        self._scrub_lock = False
        self._bar_lbl.config(text=os.path.basename(path))
        self._file_lbl.config(text=os.path.basename(path))
        self._refresh_pending_labels()
        self._refresh_cut_list()
        self._seek_to(0)
        self._status.set("Mark ranges to cut, then Export Stitched.")

    def _release_cap(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    # ── Frame navigation ───────────────────────────────────────────────────────

    def _seek(self, delta: int) -> None:
        if self._cap is None:
            return
        self._seek_to(max(0, min(self._cur_frame + delta, self._total - 1)))

    def _seek_to(self, frame_idx: int) -> None:
        if self._cap is None:
            return
        self._cur_frame = frame_idx
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, raw = self._cap.read()
        if not ok:
            return
        self._cur_raw = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
        self._render_frame()
        tc = _frames_to_tc(frame_idx, self._fps)
        self._frame_lbl.config(text=f"Frame {frame_idx + 1} / {self._total}  ({tc})")
        self._scrub_lock = True
        self._scrubber_var.set(frame_idx)
        self._scrub_lock = False
        self._draw_marker_overlay()

    def _on_scrub(self, val) -> None:
        if self._scrub_lock or self._cap is None:
            return
        self._seek_to(int(float(val)))

    def _render_frame(self) -> None:
        if self._cur_raw is None:
            self._draw_placeholder()
            return
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        if cw < 4 or ch < 4:
            self.root.after(60, self._render_frame)
            return
        oh, ow = self._cur_raw.shape[:2]
        ratio = min(cw / ow, ch / oh)
        nw, nh = int(ow * ratio), int(oh * ratio)
        off_x, off_y = (cw - nw) // 2, (ch - nh) // 2
        img = Image.fromarray(self._cur_raw).resize((nw, nh), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        self._canvas.delete("all")
        self._canvas.create_image(off_x, off_y, anchor=tk.NW, image=self._photo)

    # ── Marker overlay ──────────────────────────────────────────────────────────

    def _draw_marker_overlay(self) -> None:
        if self._cap is None:
            return
        total = max(1, self._total - 1)
        mc = self._marker_canvas
        mc.delete("all")
        w = mc.winfo_width()
        if w < 4:
            return
        # Committed cut ranges in red.
        for s, e in self._cuts:
            sx = int(s / total * w)
            ex = int(e / total * w)
            mc.create_rectangle(sx, 0, max(ex, sx + 1), 14, fill=RED, outline="")
        # Pending range outline (purple start, yellow end).
        if self._pending_start is not None:
            x = int(self._pending_start / total * w)
            mc.create_line(x, 0, x, 14, fill=PURPLE, width=2)
        if self._pending_end is not None:
            x = int(self._pending_end / total * w)
            mc.create_line(x, 0, x, 14, fill=YELLOW, width=2)

    # ── Cut range editing ───────────────────────────────────────────────────────

    def _set_cut_start(self) -> None:
        if self._cap is None:
            return
        if self._pending_end is not None and self._cur_frame >= self._pending_end:
            messagebox.showwarning("Invalid Point", "Cut start must be before cut end.")
            return
        self._pending_start = self._cur_frame
        self._refresh_pending_labels()
        self._draw_marker_overlay()

    def _set_cut_end(self) -> None:
        if self._cap is None:
            return
        if self._pending_start is not None and self._cur_frame <= self._pending_start:
            messagebox.showwarning("Invalid Point", "Cut end must be after cut start.")
            return
        self._pending_end = self._cur_frame
        self._refresh_pending_labels()
        self._draw_marker_overlay()

    def _add_cut(self) -> None:
        if self._cap is None:
            return
        if self._pending_start is None or self._pending_end is None:
            messagebox.showwarning("Incomplete Range",
                                   "Set both a cut start and a cut end first.")
            return
        self._cuts.append((self._pending_start, self._pending_end))
        self._cuts.sort()
        self._pending_start = None
        self._pending_end = None
        self._refresh_pending_labels()
        self._refresh_cut_list()
        self._draw_marker_overlay()

    def _remove_cut(self) -> None:
        sel = self._cut_list.curselection()
        if not sel:
            return
        for i in sorted(sel, reverse=True):
            del self._cuts[i]
        self._refresh_cut_list()
        self._draw_marker_overlay()

    def _clear_cuts(self) -> None:
        self._cuts = []
        self._pending_start = None
        self._pending_end = None
        self._refresh_pending_labels()
        self._refresh_cut_list()
        self._draw_marker_overlay()

    def _refresh_pending_labels(self) -> None:
        ps, pe = self._pending_start, self._pending_end
        self._cut_start_lbl.config(
            text=f"{_frames_to_tc(ps, self._fps)}" if ps is not None else "(none)")
        self._cut_end_lbl.config(
            text=f"{_frames_to_tc(pe, self._fps)}" if pe is not None else "(none)")

    def _refresh_cut_list(self) -> None:
        self._cut_list.delete(0, tk.END)
        for i, (s, e) in enumerate(self._cuts, 1):
            dur = (e - s + 1) / max(self._fps, 0.001)
            self._cut_list.insert(
                tk.END,
                f"{i}. {_frames_to_tc(s, self._fps)} → {_frames_to_tc(e, self._fps)}  (−{dur:.2f}s)")
        # Summarise how much survives.
        keeps = self._keep_segments()
        if self._cap is not None:
            kept_frames = sum(e - s + 1 for s, e in keeps)
            kept_s = kept_frames / max(self._fps, 0.001)
            self._status.set(
                f"{len(self._cuts)} cut(s) — {len(keeps)} segment(s) kept, ~{kept_s:.2f}s remaining.")

    # ── Keep-segment maths ──────────────────────────────────────────────────────

    def _keep_segments(self) -> list:
        """Return the (start, end) frame ranges that survive after the cuts.

        Cut ranges are merged (overlapping/adjacent collapse) and inverted
        against the full [0, total-1] frame range. Both ends are inclusive.
        """
        if self._total <= 0:
            return []
        last = self._total - 1
        merged: list = []
        for s, e in sorted(self._cuts):
            s = max(0, s)
            e = min(last, e)
            if e < s:
                continue
            if merged and s <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        keeps: list = []
        prev = 0
        for s, e in merged:
            if s > prev:
                keeps.append((prev, s - 1))
            prev = e + 1
        if prev <= last:
            keeps.append((prev, last))
        return keeps

    # ── Export ──────────────────────────────────────────────────────────────────

    def _start_export(self) -> None:
        if self._running:
            return
        if self._cap is None or not self._path:
            messagebox.showwarning("No File", "Load a video first.")
            return
        if not self._cuts:
            messagebox.showwarning("No Cuts", "Add at least one cut range to remove.")
            return
        keeps = self._keep_segments()
        if not keeps:
            messagebox.showwarning("Nothing Left",
                                   "The cut ranges cover the whole video — nothing would remain.")
            return
        if not _ffmpeg_available():
            messagebox.showerror(
                "ffmpeg Required",
                "Cut & Stitch needs ffmpeg on PATH to splice segments together.")
            return
        self._running = True
        self._export_btn.config(state=tk.DISABLED)
        self._progress.set(0)
        fmt_key = self._fmt_var.get()
        self._status.set(f"Stitching {len(keeps)} segment(s)…")
        threading.Thread(
            target=self._do_export,
            args=(self._path, keeps, self._fps, fmt_key),
            daemon=True).start()

    def _do_export(self, src: str, keeps: list, fps: float, fmt_key: str) -> None:
        try:
            fmt_args, out_ext = CLIP_FORMATS.get(fmt_key, list(CLIP_FORMATS.values())[0])
            # filter_complex re-encodes, so a stream-copy choice can't apply;
            # fall back to a safe re-encode format (matches the Trim tab).
            if "-c" in fmt_args and "copy" in fmt_args:
                fmt_args, out_ext = list(OUTPUT_FORMATS.values())[0]

            cap_fps = fps if fps > 0 else 30.0
            has_audio = _has_audio(src)

            # Build a trim/concat filter graph: one trim per surviving segment,
            # then concat them in order. End time is (end_frame + 1)/fps so the
            # marked end frame is included.
            parts = []
            for i, (s, e) in enumerate(keeps):
                st = s / cap_fps
                en = (e + 1) / cap_fps
                parts.append(
                    f"[0:v]trim=start={st:.6f}:end={en:.6f},setpts=PTS-STARTPTS[v{i}];")
                if has_audio:
                    parts.append(
                        f"[0:a]atrim=start={st:.6f}:end={en:.6f},asetpts=PTS-STARTPTS[a{i}];")
            n = len(keeps)
            if has_audio:
                streams = "".join(f"[v{i}][a{i}]" for i in range(n))
                parts.append(f"{streams}concat=n={n}:v=1:a=1[outv][outa]")
                maps = ["-map", "[outv]", "-map", "[outa]"]
            else:
                streams = "".join(f"[v{i}]" for i in range(n))
                parts.append(f"{streams}concat=n={n}:v=1:a=0[outv]")
                maps = ["-map", "[outv]"]
            filtergraph = "".join(parts)

            out_dir = os.path.join(os.path.dirname(os.path.abspath(src)), "cut")
            os.makedirs(out_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(src))[0]
            out_path = os.path.join(out_dir, base + "_cut" + out_ext)

            self.root.after(0, self._progress.set, 15)
            _run_ffmpeg(["-i", src, "-filter_complex", filtergraph]
                        + maps + fmt_args + [out_path])
            self.root.after(0, self._on_export_done, out_path)
        except RuntimeError as exc:
            self.root.after(0, messagebox.showerror, "ffmpeg error",
                            f"Stitch failed:\n{exc}")
            self.root.after(0, self._on_export_failed)
        except Exception as exc:
            self.root.after(0, messagebox.showerror, "Export Error",
                            f"Unexpected failure:\n{exc}")
            self.root.after(0, self._on_export_failed)

    def _on_export_done(self, out_path: str) -> None:
        self._running = False
        self._export_btn.config(state=tk.NORMAL)
        self._progress.set(100)
        self._status.set(f"Done!  Saved to {os.path.dirname(out_path)}")
        _play_cash_register()
        messagebox.showinfo(
            "Cut & Stitch Complete",
            f"Stitched video saved to:\n{out_path}")

    def _on_export_failed(self) -> None:
        self._running = False
        self._export_btn.config(state=tk.NORMAL)
        self._progress.set(0)
        self._status.set("Export failed. See error details.")


# ════════════════════════════════════════════════════════════════════════════
# Batch Crop Tab  (one crop per video, same filename)
# ════════════════════════════════════════════════════════════════════════════

class BatchCropTab:
    """
    Load multiple videos, draw one crop region on each, then export them all
    into a 'cropped' sub-folder next to each source — keeping the original name.
    """

    def __init__(self, parent: tk.Frame, root: tk.Tk) -> None:
        self.parent = parent
        self.root = root

        # Each entry: {path, total, fps, crop_rect (None or (x1,y1,x2,y2)), preview_frame}
        self._files: list = []
        self._cur_idx: int = -1
        self._photo = None

        # Canvas ↔ original-pixel mapping
        self.scale_x: float = 1.0
        self.scale_y: float = 1.0
        self.off_x: int = 0
        self.off_y: int = 0

        # Drawing state
        self._drawing = False
        self._drag_start = (0, 0)
        self._drag_rect_id = None
        self._crop_overlay_ids: tuple = ()

        # Interaction mode: "draw" (rubber-band a new rect) or "move" (drag the
        # existing rect around). Toggled by the Mode button.
        self._mode = "draw"
        self._moving = False
        self._move_anchor = (0, 0)
        self._move_orig_rect = None

        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Toolbar
        bar = tk.Frame(self.parent, bg=BG_DARK, pady=7)
        bar.pack(fill=tk.X)

        tk.Button(bar, text="  Add Videos  ", command=self._add_files,
                  bg=ACCENT, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=10)
        tk.Button(bar, text="  Remove Selected  ", command=self._remove_selected,
                  bg=RED, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=2)
        self._bar_lbl = tk.Label(bar, text="No files loaded",
                                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 10))
        self._bar_lbl.pack(side=tk.LEFT, padx=10)

        # Body
        body = tk.Frame(self.parent, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # ── Left: file queue ────────────────────────────────────────────────
        lf = tk.Frame(body, bg=BG_DARK, width=320)
        lf.pack(side=tk.LEFT, fill=tk.Y)
        lf.pack_propagate(False)

        tk.Label(lf, text="Video Queue", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(12, 6))

        lb_wrap = tk.Frame(lf, bg=BG_DARK)
        lb_wrap.pack(fill=tk.BOTH, expand=True, padx=6)
        lb_sb = tk.Scrollbar(lb_wrap, orient=tk.VERTICAL)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree = _build_queue_tree(lb_wrap, lb_sb,
                                       extra_cols=(("crop", "Crop", 46),))
        self._tree.bind("<<TreeviewSelect>>", self._on_file_select)
        tk.Label(lf, text="Ctrl/Shift-click to select multiple, then Remove Selected.",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 8),
                 wraplength=300, justify=tk.LEFT).pack(padx=8, pady=(4, 2), anchor="w")

        # ── Center: preview canvas ──────────────────────────────────────────
        center = tk.Frame(body, bg=BG)
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8)

        cf = tk.Frame(center, bg=BG_MID, bd=1, relief=tk.SOLID)
        cf.pack(fill=tk.BOTH, expand=True)
        self._canvas = tk.Canvas(cf, bg=BG_DARK, cursor="crosshair", highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.bind("<ButtonPress-1>",   self._on_press)
        self._canvas.bind("<B1-Motion>",       self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<Configure>",       lambda _e: self._render_frame())

        # Mode toggle + hint label under canvas
        hint_row = tk.Frame(center, bg=BG)
        hint_row.pack(fill=tk.X, pady=(4, 0))
        self._mode_btn = tk.Button(
            hint_row, text="  Mode: Draw ✎  ", command=self._toggle_mode,
            bg=PURPLE, fg=BG_DARK, font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, padx=4)
        self._mode_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._hint_lbl = tk.Label(hint_row, text="Draw a crop rectangle on the preview",
                                  bg=BG, fg=FG_DIM, font=("Segoe UI", 9))
        self._hint_lbl.pack(side=tk.LEFT)

        # ── Right: controls ─────────────────────────────────────────────────
        rf = tk.Frame(body, bg=BG_DARK, width=265)
        rf.pack(side=tk.RIGHT, fill=tk.Y)
        rf.pack_propagate(False)

        tk.Label(rf, text="Batch Crop", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 12, "bold")).pack(pady=(14, 4))

        tk.Label(rf, text="Draw one crop per video.\n"
                          "Output keeps the same filename\n"
                          "in a 'cropped' sub-folder.",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9),
                 wraplength=240, justify=tk.LEFT).pack(padx=8, pady=(0, 6))

        _separator(rf)

        # Current file info
        self._file_lbl = tk.Label(rf, text="— no file selected —",
                                  bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9), wraplength=240)
        self._file_lbl.pack(padx=8, pady=(6, 2))

        self._crop_info_lbl = tk.Label(rf, text="No crop defined",
                                       bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9))
        self._crop_info_lbl.pack(padx=8, pady=(0, 6))

        tk.Button(rf, text="Apply This Crop to All Files",
              command=self._apply_current_crop_to_all,
              bg=BG_MID, fg=FG_DIM, font=("Segoe UI", 9),
              relief=tk.FLAT, pady=3).pack(padx=10, pady=(2, 2), fill=tk.X)

        tk.Button(rf, text="Clear Crop for This File", command=self._clear_crop,
                  bg=BG_MID, fg=FG_DIM, font=("Segoe UI", 9),
                  relief=tk.FLAT, pady=3).pack(padx=10, pady=(2, 2), fill=tk.X)

        # Queue summary
        _separator(rf)
        tk.Label(rf, text="Crop Queue Summary", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(pady=(8, 4))
        q_wrap = tk.Frame(rf, bg=BG_DARK)
        q_wrap.pack(fill=tk.BOTH, expand=True, padx=8)
        q_sb = tk.Scrollbar(q_wrap, orient=tk.VERTICAL)
        q_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._queue_text = tk.Text(q_wrap, yscrollcommand=q_sb.set,
                                   bg=BG_MID, fg=FG, font=("Segoe UI", 8),
                                   relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD,
                                   highlightthickness=0, borderwidth=0)
        self._queue_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        q_sb.config(command=self._queue_text.yview)

        # Format selector
        _separator(rf)
        cfmt_row = tk.Frame(rf, bg=BG_DARK)
        cfmt_row.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(cfmt_row, text="Output Format:", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._fmt_var = tk.StringVar(value=CROP_FORMAT_NAMES[0])
        ttk.Combobox(cfmt_row, textvariable=self._fmt_var,
                     values=CROP_FORMAT_NAMES, state="readonly",
                     font=("Segoe UI", 9)).pack(fill=tk.X, pady=(2, 0))

        self._norm_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            rf, text="Normalize brightness", variable=self._norm_var,
            bg=BG_DARK, fg=FG, selectcolor=BG_MID,
            activebackground=BG_DARK, activeforeground=FG,
            font=("Segoe UI", 9),
        ).pack(padx=12, anchor="w", pady=(6, 0))

        # Progress + CROP ALL
        _separator(rf)
        self._progress = tk.DoubleVar(value=0)
        ttk.Progressbar(rf, variable=self._progress, maximum=100,
                        style="BatchCrop.Horizontal.TProgressbar", length=245
                        ).pack(padx=10, pady=(8, 3))
        tk.Button(rf, text="  CROP ALL", command=self._start_crop_thread,
                  bg=GREEN, fg=BG_DARK, font=("Segoe UI", 13, "bold"),
                  relief=tk.FLAT, pady=11).pack(fill=tk.X, padx=10, pady=(3, 4))
        self._status = tk.StringVar(value="Add video files to begin.")
        tk.Label(rf, textvariable=self._status, bg=BG_DARK, fg=FG_DIM,
                 font=("Segoe UI", 9), wraplength=245, justify=tk.LEFT
                 ).pack(padx=10, anchor="w", pady=(0, 8))

        # Keyboard shortcuts for file navigation within this tab
        self._tree.bind("<Up>",   lambda e: (self._nav_file(-1), "break")[-1])
        self._tree.bind("<Down>", lambda e: (self._nav_file(1),  "break")[-1])

        self.root.after(150, self._draw_placeholder)

    # ── Placeholder ───────────────────────────────────────────────────────────

    def _draw_placeholder(self) -> None:
        self._canvas.delete("placeholder")
        w, h = self._canvas.winfo_width(), self._canvas.winfo_height()
        self._canvas.create_text(w // 2, h // 2,
                                 text="Add videos and select one to preview",
                                 fill="#45475a", font=("Segoe UI", 16), tags="placeholder")

    # ── File management ───────────────────────────────────────────────────────

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Select video files", filetypes=VIDEO_FILETYPES)
        if not paths:
            return
        existing = {f["path"] for f in self._files}
        added = 0
        for p in paths:
            if p in existing:
                continue
            cap = cv2.VideoCapture(p)
            if not cap.isOpened():
                cap.release()
                continue
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            if total <= 0:
                cap.release()
                continue
            # Grab a frame from the middle third for preview
            lo, hi = total // 3, (total * 2) // 3
            target = random.randint(lo, hi) if hi > lo else max(0, total // 2)
            cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, raw = cap.read()
            cap.release()
            if not ok or raw is None:
                continue
            preview = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
            self._files.append({
                "path": p, "total": total, "fps": fps,
                "crop_rect": None, "preview_frame": preview,
            })
            added += 1
        if added:
            self._refresh_list()
            self._bar_lbl.config(text=f"{len(self._files)} file(s) loaded")
            self._update_queue_summary()
            self._status.set(f"Added {added} file(s). Select one to draw a crop.")
            if self._cur_idx == -1 and self._files:
                self._tree.selection_set("0")
                self._tree.focus("0")
                self._load_file(0)

    def _refresh_list(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for i, f in enumerate(self._files):
            self._tree.insert("", "end", iid=str(i),
                              text=os.path.basename(f["path"]),
                              values=(_folder_of(f["path"]),
                                      "✓" if f["crop_rect"] is not None else ""))
        if self._cur_idx >= 0:
            self._tree.selection_set(str(self._cur_idx))
            self._tree.focus(str(self._cur_idx))

    def _remove_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        cur_path = self._files[self._cur_idx]["path"] if self._cur_idx >= 0 else None
        for idx in sorted((int(i) for i in sel), reverse=True):
            self._files.pop(idx)
        self._cur_idx = next((i for i, f in enumerate(self._files)
                              if f["path"] == cur_path), -1) if cur_path else -1
        if self._cur_idx < 0:
            self._photo = None
            self._crop_overlay_ids = ()
            self._canvas.delete("all")
            self._draw_placeholder()
            self._file_lbl.config(text="— no file selected —")
            self._crop_info_lbl.config(text="No crop defined")
        self._refresh_list()
        self._bar_lbl.config(
            text=f"{len(self._files)} file(s) loaded" if self._files else "No files loaded")
        self._update_queue_summary()

    # ── File selection ────────────────────────────────────────────────────────

    def _on_file_select(self, _=None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx == self._cur_idx:
            return
        self._load_file(idx)

    def _load_file(self, idx: int) -> None:
        self._cur_idx = idx
        entry = self._files[idx]
        self._file_lbl.config(text=os.path.basename(entry["path"]))
        self._refresh_crop_info()
        self._render_frame()

    def _nav_file(self, delta: int) -> None:
        if not self._files:
            return
        if self._cur_idx < 0:
            new_idx = 0
        else:
            new_idx = max(0, min(self._cur_idx + delta, len(self._files) - 1))
        if new_idx == self._cur_idx:
            return
        self._tree.selection_set(str(new_idx))
        self._tree.focus(str(new_idx))
        self._tree.see(str(new_idx))
        self._load_file(new_idx)

    # ── Frame rendering ───────────────────────────────────────────────────────

    def _render_frame(self) -> None:
        if self._cur_idx < 0:
            self._draw_placeholder()
            return
        entry = self._files[self._cur_idx]
        frame = entry["preview_frame"]
        if frame is None:
            self._draw_placeholder()
            return
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        if cw < 4 or ch < 4:
            self.root.after(60, self._render_frame)
            return
        oh, ow = frame.shape[:2]
        ratio = min(cw / ow, ch / oh)
        nw, nh = int(ow * ratio), int(oh * ratio)
        self.scale_x = ow / nw
        self.scale_y = oh / nh
        self.off_x = (cw - nw) // 2
        self.off_y = (ch - nh) // 2
        img = Image.fromarray(frame).resize((nw, nh), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        self._canvas.delete("all")
        self._canvas.create_image(self.off_x, self.off_y, anchor=tk.NW, image=self._photo)
        # Re-draw existing crop overlay
        self._crop_overlay_ids = ()
        if entry["crop_rect"] is not None:
            self._draw_crop_overlay(entry["crop_rect"])

    # ── Coordinate helpers ────────────────────────────────────────────────────

    def _canvas_to_orig(self, cx, cy):
        entry = self._files[self._cur_idx]
        oh, ow = entry["preview_frame"].shape[:2]
        return (max(0, min(int((cx - self.off_x) * self.scale_x), ow)),
                max(0, min(int((cy - self.off_y) * self.scale_y), oh)))

    def _orig_to_canvas(self, ox, oy):
        return ox / self.scale_x + self.off_x, oy / self.scale_y + self.off_y

    # ── Mode toggle ────────────────────────────────────────────────────────────

    def _toggle_mode(self) -> None:
        self._mode = "move" if self._mode == "draw" else "draw"
        if self._mode == "move":
            self._mode_btn.config(text="  Mode: Move ✥  ")
            self._canvas.config(cursor="fleur")
            self._hint_lbl.config(text="Drag the crop rectangle to reposition it")
        else:
            self._mode_btn.config(text="  Mode: Draw ✎  ")
            self._canvas.config(cursor="crosshair")
            self._hint_lbl.config(text="Draw a crop rectangle on the preview")

    def _point_in_crop(self, cx, cy) -> bool:
        """True if canvas point (cx, cy) lies inside the current crop overlay."""
        if self._cur_idx < 0:
            return False
        rect = self._files[self._cur_idx]["crop_rect"]
        if rect is None:
            return False
        ox1, oy1, ox2, oy2 = rect
        bx1, by1 = self._orig_to_canvas(ox1, oy1)
        bx2, by2 = self._orig_to_canvas(ox2, oy2)
        return min(bx1, bx2) <= cx <= max(bx1, bx2) and min(by1, by2) <= cy <= max(by1, by2)

    # ── Mouse events (draw one crop rectangle) ────────────────────────────────

    def _on_press(self, event) -> None:
        if self._cur_idx < 0:
            return
        if self._mode == "move":
            if not self._point_in_crop(event.x, event.y):
                return
            self._moving = True
            self._move_anchor = (event.x, event.y)
            self._move_orig_rect = self._files[self._cur_idx]["crop_rect"]
            return
        self._drawing = True
        self._drag_start = (event.x, event.y)
        self._drag_rect_id = self._canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#ffffff", width=2, dash=(5, 3))

    def _on_drag(self, event) -> None:
        if self._moving:
            entry = self._files[self._cur_idx]
            ax, ay = self._move_anchor
            dox = int((event.x - ax) * self.scale_x)
            doy = int((event.y - ay) * self.scale_y)
            new_rect = _shift_rect_clamped(
                self._move_orig_rect, dox, doy, entry["preview_frame"].shape[:2])
            entry["crop_rect"] = new_rect
            for cid in self._crop_overlay_ids:
                self._canvas.delete(cid)
            self._crop_overlay_ids = ()
            self._draw_crop_overlay(new_rect)
            self._refresh_crop_info()
            return
        if self._drawing and self._drag_rect_id is not None:
            x0, y0 = self._drag_start
            self._canvas.coords(self._drag_rect_id, x0, y0, event.x, event.y)

    def _on_release(self, event) -> None:
        if self._moving:
            self._moving = False
            self._move_orig_rect = None
            self._update_queue_summary()
            self._update_listbox_label(self._cur_idx)
            return
        if not self._drawing:
            return
        self._drawing = False
        if self._drag_rect_id is not None:
            self._canvas.delete(self._drag_rect_id)
            self._drag_rect_id = None
        x0, y0 = self._drag_start
        cx1, cy1 = min(x0, event.x), min(y0, event.y)
        cx2, cy2 = max(x0, event.x), max(y0, event.y)
        if (cx2 - cx1) < 10 or (cy2 - cy1) < 10:
            return
        ox1, oy1 = self._canvas_to_orig(cx1, cy1)
        ox2, oy2 = self._canvas_to_orig(cx2, cy2)
        entry = self._files[self._cur_idx]
        entry["crop_rect"] = (ox1, oy1, ox2, oy2)
        # Remove old overlay, draw new one
        for cid in self._crop_overlay_ids:
            self._canvas.delete(cid)
        self._crop_overlay_ids = ()
        self._draw_crop_overlay(entry["crop_rect"])
        self._refresh_crop_info()
        self._update_queue_summary()
        self._update_listbox_label(self._cur_idx)

    # ── Overlay ───────────────────────────────────────────────────────────────

    def _draw_crop_overlay(self, rect: tuple) -> None:
        color = GREEN
        ox1, oy1, ox2, oy2 = rect
        cx1, cy1 = self._orig_to_canvas(ox1, oy1)
        cx2, cy2 = self._orig_to_canvas(ox2, oy2)
        rid = self._canvas.create_rectangle(cx1, cy1, cx2, cy2, outline=color, width=2)
        w, h = ox2 - ox1, oy2 - oy1
        txt = self._canvas.create_text(cx1 + 4, cy1 - 4,
                                       text=f"{w}×{h}", anchor="sw",
                                       fill=color, font=("Segoe UI", 9, "bold"))
        self._crop_overlay_ids = (rid, txt)

    # ── Info helpers ──────────────────────────────────────────────────────────

    def _refresh_crop_info(self) -> None:
        if self._cur_idx < 0:
            self._crop_info_lbl.config(text="No crop defined")
            return
        entry = self._files[self._cur_idx]
        r = entry["crop_rect"]
        if r is None:
            self._crop_info_lbl.config(text="No crop defined — draw one on the preview")
        else:
            x1, y1, x2, y2 = r
            self._crop_info_lbl.config(text=f"Crop: {x2-x1}×{y2-y1} at ({x1},{y1})")

    def _update_listbox_label(self, idx: int) -> None:
        """Show a ✓ in the Crop column for files that have a crop defined."""
        entry = self._files[idx]
        self._tree.set(str(idx), "crop",
                       "✓" if entry["crop_rect"] is not None else "")

    def _clear_crop(self) -> None:
        if self._cur_idx < 0:
            return
        entry = self._files[self._cur_idx]
        entry["crop_rect"] = None
        for cid in self._crop_overlay_ids:
            self._canvas.delete(cid)
        self._crop_overlay_ids = ()
        self._refresh_crop_info()
        self._update_queue_summary()
        self._update_listbox_label(self._cur_idx)

    def _apply_current_crop_to_all(self) -> None:
        if self._cur_idx < 0:
            return
        crop_rect = self._files[self._cur_idx]["crop_rect"]
        if crop_rect is None:
            messagebox.showwarning("No Crop", "Draw a crop on the selected file first.")
            return
        for idx, entry in enumerate(self._files):
            entry["crop_rect"] = tuple(crop_rect)
            self._update_listbox_label(idx)
        self._refresh_crop_info()
        self._update_queue_summary()
        self._status.set(f"Applied crop to all {len(self._files)} file(s).")

    def _update_queue_summary(self) -> None:
        self._queue_text.config(state=tk.NORMAL)
        self._queue_text.delete("1.0", tk.END)
        defined = 0
        for i, entry in enumerate(self._files, 1):
            name = os.path.basename(entry["path"])
            r = entry["crop_rect"]
            if r:
                x1, y1, x2, y2 = r
                self._queue_text.insert(
                    tk.END, f"{i}. {name}\n   ✓ {x2-x1}×{y2-y1} at ({x1},{y1})\n\n")
                defined += 1
            else:
                self._queue_text.insert(tk.END, f"{i}. {name}\n   — no crop —\n\n")
        self._queue_text.config(state=tk.DISABLED)
        self._status.set(f"{defined}/{len(self._files)} file(s) have crops defined.")

    # ── Processing ────────────────────────────────────────────────────────────

    def _start_crop_thread(self) -> None:
        if not self._files:
            messagebox.showwarning("No Files", "Add at least one video file first.")
            return
        jobs = [(f["path"], f["crop_rect"]) for f in self._files if f["crop_rect"] is not None]
        if not jobs:
            messagebox.showwarning("No Crops", "Draw a crop region on at least one video.")
            return
        self._progress.set(0)
        fmt_key = self._fmt_var.get()
        engine = f"ffmpeg — {fmt_key}" if _ffmpeg_available() else "OpenCV fallback"
        self._status.set(f"Cropping… [{engine}]")
        threading.Thread(target=self._do_crop, args=(jobs, fmt_key), daemon=True).start()

    def _do_crop(self, jobs: list, fmt_key: str) -> None:
        if _ffmpeg_available():
            self._do_crop_ffmpeg(jobs, fmt_key)
        else:
            self._do_crop_opencv(jobs)

    def _do_crop_ffmpeg(self, jobs: list, fmt_key: str) -> None:
        fmt_args, out_ext = OUTPUT_FORMATS.get(fmt_key, list(OUTPUT_FORMATS.values())[0])
        total = len(jobs)
        normalize = self._norm_var.get()
        luma_map: dict = {}
        norm_target = 128.0
        if normalize:
            self.root.after(0, self._status.set, "Measuring brightness\u2026")
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, total)) as exe:
                futures = {exe.submit(_measure_luma, src): src for src, _ in jobs}
                for fut in concurrent.futures.as_completed(futures):
                    luma_map[futures[fut]] = fut.result()
            measured = list(luma_map.values())
            norm_target = sum(measured) / len(measured)
        for i, (src, rect) in enumerate(jobs, 1):
            x1, y1, x2, y2 = rect
            w = (x2 - x1) & ~1
            h = (y2 - y1) & ~1
            src_dir = os.path.dirname(os.path.abspath(src))
            out_dir = os.path.join(src_dir, "cropped")
            os.makedirs(out_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(src))[0]
            out_path = os.path.join(out_dir, base + out_ext)
            self.root.after(0, self._status.set,
                            f"Cropping {i}/{total}: {os.path.basename(src)}")
            vf = f"yadif=0:-1:0,crop={w}:{h}:{x1}:{y1}"
            if normalize:
                luma = max(luma_map.get(src, 128.0), 1.0)
                factor = max(0.05, min(norm_target / luma, 20.0))
                vf += f",format=yuv420p,lutyuv=y='clip(val*{factor:.6f},0,255)'"
            try:
                _run_ffmpeg(
                    ["-hwaccel", "auto", "-i", src, "-vf", vf]
                    + fmt_args + [out_path]
                )
            except RuntimeError as exc:
                self.root.after(0, messagebox.showerror, "ffmpeg error",
                                f"{os.path.basename(src)} failed:\n{exc}")
                return
            self.root.after(0, self._progress.set, i / total * 100)
        self.root.after(0, self._on_crop_done, total)

    def _do_crop_opencv(self, jobs: list) -> None:
        total = len(jobs)
        for job_n, (src, rect) in enumerate(jobs, 1):
            x1, y1, x2, y2 = rect
            cw, ch = x2 - x1, y2 - y1
            src_dir = os.path.dirname(os.path.abspath(src))
            out_dir = os.path.join(src_dir, "cropped")
            os.makedirs(out_dir, exist_ok=True)
            ext = os.path.splitext(src)[1].lower()
            base = os.path.splitext(os.path.basename(src))[0]
            out_path = os.path.join(out_dir, base + ext)
            cap = cv2.VideoCapture(src)
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
            fourcc = _fourcc(ext)
            writer = cv2.VideoWriter(out_path, fourcc, fps, (cw, ch))
            self.root.after(0, self._status.set,
                            f"Cropping {job_n}/{total}: {os.path.basename(src)}")
            for fi in range(total_frames):
                ok, frame = cap.read()
                if not ok:
                    break
                writer.write(frame[y1:y2, x1:x2])
                if fi % 120 == 0:
                    overall = (job_n - 1 + (fi + 1) / max(1, total_frames)) / total * 100
                    self.root.after(0, self._progress.set, overall)
            cap.release()
            writer.release()
            self.root.after(0, self._progress.set, job_n / total * 100)
        self.root.after(0, self._on_crop_done, total)

    def _on_crop_done(self, count: int) -> None:
        self._progress.set(100)
        self._status.set(f"Done!  {count} file(s) cropped.")
        _play_cash_register()
        messagebox.showinfo(
            "Batch Crop Complete",
            f"{count} file(s) cropped.\n"
            "Each output is in a 'cropped' folder next to its source video.")


# ════════════════════════════════════════════════════════════════════════════
# Brightness Normalisation Tab
# ════════════════════════════════════════════════════════════════════════════

class BrightnessTab:
    """
    Load multiple videos, sample 3 frames each from the middle third to
    measure mean luma, then re-encode every file with a per-file brightness
    correction that brings them all to the same target luminance.

    Analysis is parallelised with a ThreadPoolExecutor (I/O-bound reads).
    Encoding is parallelised with up to 4 concurrent ffmpeg processes.
    """

    def __init__(self, parent: tk.Frame, root: tk.Tk) -> None:
        self.parent = parent
        self.root = root

        # {path, total, fps, brightness: float|None, sample_frames: list[ndarray]}
        self._files: list = []
        self._cur_idx: int = -1
        self._photo = None
        self._analyzing = False
        self._normalizing = False

        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        bar = tk.Frame(self.parent, bg=BG_DARK, pady=7)
        bar.pack(fill=tk.X)

        tk.Button(bar, text="  Add Videos  ", command=self._add_files,
                  bg=ACCENT, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=10)
        tk.Button(bar, text="  Remove Selected  ", command=self._remove_selected,
                  bg=RED, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=2)
        self._bar_lbl = tk.Label(bar, text="No files loaded",
                                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 10))
        self._bar_lbl.pack(side=tk.LEFT, padx=10)

        body = tk.Frame(self.parent, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # ── Left: file queue ──────────────────────────────────────────────────
        lf = tk.Frame(body, bg=BG_DARK, width=320)
        lf.pack(side=tk.LEFT, fill=tk.Y)
        lf.pack_propagate(False)

        tk.Label(lf, text="Video Queue", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(12, 6))

        lb_wrap = tk.Frame(lf, bg=BG_DARK)
        lb_wrap.pack(fill=tk.BOTH, expand=True, padx=6)
        lb_sb = tk.Scrollbar(lb_wrap, orient=tk.VERTICAL)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree = _build_queue_tree(lb_wrap, lb_sb,
                                       extra_cols=(("luma", "Luma", 56),))
        self._tree.bind("<<TreeviewSelect>>", self._on_file_select)
        tk.Label(lf, text="Ctrl/Shift-click to select multiple, then Remove Selected.",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 8),
                 wraplength=300, justify=tk.LEFT).pack(padx=8, pady=(4, 2), anchor="w")

        # ── Center: 3 sample-frame preview ────────────────────────────────────
        center = tk.Frame(body, bg=BG)
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8)

        cf = tk.Frame(center, bg=BG_MID, bd=1, relief=tk.SOLID)
        cf.pack(fill=tk.BOTH, expand=True)
        self._canvas = tk.Canvas(cf, bg=BG_DARK, highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.bind("<Configure>", lambda _: self._render_frame())

        self._hint_lbl = tk.Label(
            center,
            text="Select an analyzed file to preview its 3 sample frames",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 9))
        self._hint_lbl.pack(pady=(4, 0))

        # ── Right: controls ───────────────────────────────────────────────────
        rf = tk.Frame(body, bg=BG_DARK, width=290)
        rf.pack(side=tk.RIGHT, fill=tk.Y)
        rf.pack_propagate(False)

        tk.Label(rf, text="Brightness Normalization",
                 bg=BG_DARK, fg=FG, font=("Segoe UI", 11, "bold")).pack(pady=(14, 2))
        tk.Label(rf, text="Equalise luminance across videos.\n"
                          "3 frames sampled per file (parallel).",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9),
                 wraplength=270, justify=tk.LEFT).pack(padx=8, pady=(0, 6))

        _separator(rf)

        # Target brightness row
        tgt_frame = tk.Frame(rf, bg=BG_DARK)
        tgt_frame.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(tgt_frame, text="Target brightness (0–255):", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        tgt_inner = tk.Frame(tgt_frame, bg=BG_DARK)
        tgt_inner.pack(fill=tk.X, pady=(2, 0))
        self._auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            tgt_inner, text="Auto (mean of all files)", variable=self._auto_var,
            bg=BG_DARK, fg=FG, selectcolor=BG_MID,
            activebackground=BG_DARK, activeforeground=FG,
            font=("Segoe UI", 9), command=self._on_auto_toggle,
        ).pack(side=tk.LEFT)
        self._target_entry = tk.Entry(
            tgt_inner, width=6, bg=BG_MID, fg=FG,
            insertbackground=FG, relief=tk.FLAT,
            font=("Segoe UI", 9), state=tk.DISABLED)
        self._target_entry.pack(side=tk.LEFT, padx=(6, 0))
        self._target_entry.insert(0, "128")

        _separator(rf)

        # Brightness readings
        tk.Label(rf, text="Brightness Readings", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(pady=(8, 4))
        br_wrap = tk.Frame(rf, bg=BG_DARK)
        br_wrap.pack(fill=tk.BOTH, expand=True, padx=8)
        br_sb = tk.Scrollbar(br_wrap, orient=tk.VERTICAL)
        br_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._readings_text = tk.Text(
            br_wrap, yscrollcommand=br_sb.set,
            bg=BG_MID, fg=FG, font=("Segoe UI", 8),
            relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD,
            highlightthickness=0, borderwidth=0)
        self._readings_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        br_sb.config(command=self._readings_text.yview)

        # Output format
        _separator(rf)
        fmt_row = tk.Frame(rf, bg=BG_DARK)
        fmt_row.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(fmt_row, text="Output Format:", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._fmt_var = tk.StringVar(value=CROP_FORMAT_NAMES[0])
        ttk.Combobox(fmt_row, textvariable=self._fmt_var,
                     values=CROP_FORMAT_NAMES, state="readonly",
                     font=("Segoe UI", 9)).pack(fill=tk.X, pady=(2, 0))

        _separator(rf)
        self._progress = tk.DoubleVar(value=0)
        ttk.Progressbar(rf, variable=self._progress, maximum=100,
                        style="Brightness.Horizontal.TProgressbar", length=270
                        ).pack(padx=10, pady=(8, 3))

        btn_row = tk.Frame(rf, bg=BG_DARK)
        btn_row.pack(fill=tk.X, padx=10, pady=(3, 4))
        self._analyze_btn = tk.Button(
            btn_row, text="ANALYZE", command=self._start_analyze_thread,
            bg=YELLOW, fg=BG_DARK, font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT, pady=9)
        self._analyze_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self._normalize_btn = tk.Button(
            btn_row, text="NORMALIZE ALL", command=self._start_normalize_thread,
            bg=GREEN, fg=BG_DARK, font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT, pady=9)
        self._normalize_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._status = tk.StringVar(value="Add video files and click Analyze.")
        tk.Label(rf, textvariable=self._status,
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9),
                 wraplength=270, justify=tk.LEFT
                 ).pack(padx=10, anchor="w", pady=(0, 8))

        self.root.after(150, self._draw_placeholder)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _on_auto_toggle(self) -> None:
        self._target_entry.config(
            state=tk.DISABLED if self._auto_var.get() else tk.NORMAL)

    def _draw_placeholder(self) -> None:
        self._canvas.delete("placeholder")
        w, h = self._canvas.winfo_width(), self._canvas.winfo_height()
        self._canvas.create_text(
            w // 2, h // 2,
            text="Add videos and click Analyze\nto preview sample frames",
            fill="#45475a", font=("Segoe UI", 14), tags="placeholder",
            justify=tk.CENTER, width=max(w - 40, 100))

    # ── File management ───────────────────────────────────────────────────────

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select video files", filetypes=VIDEO_FILETYPES)
        if not paths:
            return
        existing = {f["path"] for f in self._files}
        added = 0
        for p in paths:
            if p in existing:
                continue
            cap = cv2.VideoCapture(p)
            if not cap.isOpened():
                cap.release()
                continue
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            cap.release()
            if total <= 0:
                continue
            self._files.append({
                "path": p, "total": total, "fps": fps,
                "brightness": None, "sample_frames": [],
            })
            added += 1
        if not added:
            return
        self._refresh_list()
        self._bar_lbl.config(text=f"{len(self._files)} file(s) loaded")
        self._update_readings()
        self._status.set(f"Added {added} file(s). Click Analyze to measure brightness.")
        if self._cur_idx == -1 and self._files:
            self._cur_idx = 0
            self._tree.selection_set("0")
            self._tree.focus("0")
            self._render_frame()

    def _refresh_list(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for i, f in enumerate(self._files):
            b = f["brightness"]
            self._tree.insert("", "end", iid=str(i),
                              text=os.path.basename(f["path"]),
                              values=(_folder_of(f["path"]),
                                      f"{b:.1f}" if b is not None else "—"))
        if self._cur_idx >= 0:
            self._tree.selection_set(str(self._cur_idx))
            self._tree.focus(str(self._cur_idx))

    def _remove_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        cur_path = self._files[self._cur_idx]["path"] if self._cur_idx >= 0 else None
        for idx in sorted((int(i) for i in sel), reverse=True):
            self._files.pop(idx)
        self._cur_idx = next((i for i, f in enumerate(self._files)
                              if f["path"] == cur_path), -1) if cur_path else -1
        if self._cur_idx < 0:
            self._photo = None
            self._canvas.delete("all")
            self._draw_placeholder()
        self._refresh_list()
        self._bar_lbl.config(
            text=f"{len(self._files)} file(s) loaded" if self._files else "No files loaded")
        self._update_readings()

    # ── Selection / navigation ────────────────────────────────────────────────

    def _on_file_select(self, _=None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx == self._cur_idx:
            return
        self._cur_idx = idx
        self._render_frame()

    def _nav_file(self, delta: int) -> None:
        if not self._files:
            return
        new_idx = max(0, min(
            (self._cur_idx if self._cur_idx >= 0 else 0) + delta,
            len(self._files) - 1))
        if new_idx == self._cur_idx:
            return
        self._cur_idx = new_idx
        self._tree.selection_set(str(new_idx))
        self._tree.focus(str(new_idx))
        self._tree.see(str(new_idx))
        self._render_frame()

    # ── Sample-frame preview (3 frames side by side) ──────────────────────────

    def _render_frame(self) -> None:
        if self._cur_idx < 0:
            self._draw_placeholder()
            return
        entry = self._files[self._cur_idx]
        frames = entry["sample_frames"]
        if not frames:
            self._canvas.delete("all")
            self._draw_placeholder()
            return
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        if cw < 4 or ch < 4:
            self.root.after(60, self._render_frame)
            return

        n = len(frames)
        cell_w = cw // n
        composite = Image.new("RGB", (cw, ch), (0x11, 0x11, 0x1b))
        for i, frame in enumerate(frames):
            oh, ow = frame.shape[:2]
            ratio = min(cell_w / ow, ch / oh)
            nw, nh = int(ow * ratio), int(oh * ratio)
            off_x = i * cell_w + (cell_w - nw) // 2
            off_y = (ch - nh) // 2
            tile = Image.fromarray(frame).resize((nw, nh), Image.LANCZOS)
            composite.paste(tile, (off_x, off_y))

        self._photo = ImageTk.PhotoImage(composite)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)

        if entry["brightness"] is not None:
            label = f"Mean luminance: {entry['brightness']:.1f} / 255"
            self._canvas.create_rectangle(
                cw // 2 - 155, ch - 34, cw // 2 + 155, ch - 8,
                fill="#000000", stipple="gray50", outline="")
            self._canvas.create_text(
                cw // 2, ch - 20,
                text=label, fill="white", font=("Segoe UI", 10, "bold"))

    # ── Brightness analysis ───────────────────────────────────────────────────

    @staticmethod
    def _assess_one(path: str, total: int):
        """
        Worker (runs in ThreadPoolExecutor).
        Reads 3 frames spaced evenly across the middle third of the video.
        Returns (mean_Y_luma: float, [rgb_frame, ...]).
        """
        lo = total // 3
        hi = max(lo + 3, (total * 2) // 3)
        hi = min(hi, total - 1)
        span = hi - lo
        # Three evenly-spaced indices inside the middle third
        indices = [lo + span // 4, lo + span // 2, lo + (span * 3) // 4]

        cap = cv2.VideoCapture(path)
        lumas = []
        rgb_frames = []
        for fi in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if not ok:
                continue
            ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
            lumas.append(float(np.mean(ycrcb[:, :, 0])))
            rgb_frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        cap.release()

        mean_luma = float(np.mean(lumas)) if lumas else 128.0
        return mean_luma, rgb_frames

    def _start_analyze_thread(self) -> None:
        if self._analyzing or self._normalizing:
            return
        if not self._files:
            messagebox.showwarning("No Files", "Add at least one video file first.")
            return
        self._analyzing = True
        self._analyze_btn.config(state=tk.DISABLED)
        self._normalize_btn.config(state=tk.DISABLED)
        self._progress.set(0)
        self._status.set(f"Analyzing {len(self._files)} file(s) in parallel…")
        threading.Thread(target=self._do_analyze, daemon=True).start()

    def _do_analyze(self) -> None:
        files_snap = list(self._files)
        n = len(files_snap)
        results = {}
        done_count = [0]

        max_workers = min(8, max(1, n))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as exe:
            future_map = {
                exe.submit(BrightnessTab._assess_one, f["path"], f["total"]): i
                for i, f in enumerate(files_snap)
            }
            for fut in concurrent.futures.as_completed(future_map):
                i = future_map[fut]
                try:
                    brightness, frames = fut.result()
                except Exception:
                    brightness, frames = 128.0, []
                results[i] = (brightness, frames)
                done_count[0] += 1
                self.root.after(0, self._progress.set, done_count[0] / n * 100)

        def _apply() -> None:
            for i, (b, frames) in results.items():
                if i >= len(self._files):
                    continue
                self._files[i]["brightness"] = b
                self._files[i]["sample_frames"] = frames

            # Repopulate the queue (now showing each measured luma) and restore
            # the previous selection highlight.
            self._refresh_list()

            # Update auto-target to mean of all measured brightnesses
            vals = [f["brightness"] for f in self._files if f["brightness"] is not None]
            if vals:
                mean_b = sum(vals) / len(vals)
                self._target_entry.config(state=tk.NORMAL)
                self._target_entry.delete(0, tk.END)
                self._target_entry.insert(0, f"{mean_b:.1f}")
                if self._auto_var.get():
                    self._target_entry.config(state=tk.DISABLED)

            self._update_readings()
            self._progress.set(100)
            if self._cur_idx >= 0:
                self._render_frame()
            self._analyzing = False
            self._analyze_btn.config(state=tk.NORMAL)
            self._normalize_btn.config(state=tk.NORMAL)
            self._status.set(
                f"Analysis complete.  {n} file(s) measured.  "
                "Click Normalize All to process.")

        self.root.after(0, _apply)

    def _update_readings(self) -> None:
        self._readings_text.config(state=tk.NORMAL)
        self._readings_text.delete("1.0", tk.END)
        for i, f in enumerate(self._files, 1):
            name = os.path.basename(f["path"])
            b = f["brightness"]
            b_str = f"{b:.1f}" if b is not None else "—  (not analyzed)"
            self._readings_text.insert(tk.END, f"{i}. {name}\n   Luma: {b_str}\n\n")
        self._readings_text.config(state=tk.DISABLED)

    # ── Brightness normalisation (parallel ffmpeg) ────────────────────────────

    def _start_normalize_thread(self) -> None:
        if self._analyzing or self._normalizing:
            return
        if not self._files:
            messagebox.showwarning("No Files", "Add at least one video file first.")
            return
        if not _ffmpeg_available():
            messagebox.showwarning(
                "ffmpeg Missing",
                "ffmpeg must be installed and on PATH for brightness normalization.")
            return
        unanalyzed = [f for f in self._files if f["brightness"] is None]
        if unanalyzed:
            messagebox.showwarning(
                "Not Analyzed",
                f"{len(unanalyzed)} file(s) have not been analyzed yet.\n"
                "Click Analyze first.")
            return

        # Determine target luma
        if self._auto_var.get():
            vals = [f["brightness"] for f in self._files if f["brightness"] is not None]
            target = sum(vals) / len(vals) if vals else 128.0
        else:
            try:
                target = float(self._target_entry.get().strip())
                if not (1.0 <= target <= 254.0):
                    raise ValueError
            except ValueError:
                messagebox.showwarning(
                    "Invalid Target",
                    "Enter a target brightness between 1 and 254.")
                return

        fmt_key = self._fmt_var.get()
        jobs = [{"path": f["path"], "brightness": f["brightness"]}
                for f in self._files if f["brightness"] is not None]

        self._normalizing = True
        self._analyze_btn.config(state=tk.DISABLED)
        self._normalize_btn.config(state=tk.DISABLED)
        self._progress.set(0)
        self._status.set(
            f"Normalizing {len(jobs)} file(s) → target luma {target:.1f}…")
        threading.Thread(
            target=self._do_normalize,
            args=(jobs, target, fmt_key),
            daemon=True).start()

    def _do_normalize(self, jobs: list, target: float, fmt_key: str) -> None:
        fmt_args, out_ext = OUTPUT_FORMATS.get(fmt_key, list(OUTPUT_FORMATS.values())[0])
        n = len(jobs)
        done_count = [0]
        errors = []
        lock = threading.Lock()

        def _process_one(job: dict):
            src = job["path"]
            measured = max(job["brightness"], 1.0)
            factor = max(0.05, min(target / measured, 20.0))

            src_dir = os.path.dirname(os.path.abspath(src))
            out_dir = os.path.join(src_dir, "normalized")
            os.makedirs(out_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(src))[0]
            out_path = os.path.join(out_dir, base + out_ext)

            # Convert to yuv420p so lutyuv always gets the Y (luma) plane,
            # then scale only Y — chroma (colour) is unchanged.
            vf = (
                f"format=yuv420p,"
                f"lutyuv=y='clip(val*{factor:.6f},0,255)'"
            )
            try:
                _run_ffmpeg(
                    ["-hwaccel", "auto", "-i", src, "-vf", vf]
                    + fmt_args
                    + [out_path]
                )
                return None
            except RuntimeError as exc:
                return f"{os.path.basename(src)}: {exc}"

        # Run up to 4 ffmpeg processes concurrently (each uses its own CPU threads)
        max_workers = min(4, n)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as exe:
            futures = {exe.submit(_process_one, job): job for job in jobs}
            for fut in concurrent.futures.as_completed(futures):
                err = fut.result()
                with lock:
                    if err:
                        errors.append(err)
                    done_count[0] += 1
                self.root.after(0, self._progress.set, done_count[0] / n * 100)

        def _finish() -> None:
            self._normalizing = False
            self._analyze_btn.config(state=tk.NORMAL)
            self._normalize_btn.config(state=tk.NORMAL)
            self._progress.set(100)
            _play_cash_register()
            if errors:
                messagebox.showerror(
                    "Normalization Errors",
                    f"{len(errors)} file(s) failed:\n\n"
                    + "\n".join(e[:200] for e in errors[:5]))
                self._status.set(
                    f"Done with {len(errors)} error(s).  "
                    f"{n - len(errors)} file(s) succeeded.")
            else:
                self._status.set(
                    f"Done!  {n} file(s) saved to 'normalized' folder(s).")
                messagebox.showinfo(
                    "Normalization Complete",
                    f"{n} file(s) normalized to target luma {target:.1f}.\n"
                    "Output saved to a 'normalized' folder next to each source video.")

        self.root.after(0, _finish)


# ════════════════════════════════════════════════════════════════════════════
# Stretch / Resize Tab  (force every video to one exact resolution)
# ════════════════════════════════════════════════════════════════════════════

# Common target resolutions offered as one-click presets
STRETCH_PRESETS = {
    "Custom":                          None,
    "1920 × 1080  (16:9 landscape)":   (1920, 1080),
    "1080 × 1920  (9:16 portrait)":    (1080, 1920),
    "1280 × 720   (16:9 landscape)":   (1280, 720),
    "720 × 1280   (9:16 portrait)":    (720, 1280),
    "1080 × 1080  (1:1 square)":       (1080, 1080),
    "640 × 480    (4:3)":              (640, 480),
}


class StretchTab:
    """
    Load multiple videos of any resolution and re-encode each one to a single
    user-defined width × height. Aspect ratio is *intentionally* ignored
    (scale=W:H,setsar=1) so artificially squished footage is stretched back
    into shape. Output is saved to a 'stretched' folder next to each source.

    Encoding is parallelised with up to 4 concurrent ffmpeg processes.
    """

    def __init__(self, parent: tk.Frame, root: tk.Tk) -> None:
        self.parent = parent
        self.root = root
        self._files: list = []      # {path, w, h, total, fps, frame: ndarray|None}
        self._cur_idx: int = -1
        self._photo = None
        self._running = False
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        bar = tk.Frame(self.parent, bg=BG_DARK, pady=7)
        bar.pack(fill=tk.X)
        tk.Button(bar, text="  Add Videos  ", command=self._add_files,
                  bg=ACCENT, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=10)
        tk.Button(bar, text="  Remove Selected  ", command=self._remove_selected,
                  bg=RED, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=2)
        self._bar_lbl = tk.Label(bar, text="No files loaded",
                                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 10))
        self._bar_lbl.pack(side=tk.LEFT, padx=10)

        body = tk.Frame(self.parent, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # ── Left: file queue ──────────────────────────────────────────────────
        lf = tk.Frame(body, bg=BG_DARK, width=320)
        lf.pack(side=tk.LEFT, fill=tk.Y)
        lf.pack_propagate(False)
        tk.Label(lf, text="Video Queue", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(12, 6))
        lb_wrap = tk.Frame(lf, bg=BG_DARK)
        lb_wrap.pack(fill=tk.BOTH, expand=True, padx=6)
        lb_sb = tk.Scrollbar(lb_wrap, orient=tk.VERTICAL)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree = _build_queue_tree(lb_wrap, lb_sb,
                                       extra_cols=(("res", "Res", 80),))
        self._tree.bind("<<TreeviewSelect>>", self._on_file_select)
        tk.Label(lf, text="Ctrl/Shift-click to select multiple, then Remove Selected.",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 8),
                 wraplength=300, justify=tk.LEFT).pack(padx=8, pady=(4, 2), anchor="w")

        # ── Center: stretched preview ─────────────────────────────────────────
        center = tk.Frame(body, bg=BG)
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8)
        cf = tk.Frame(center, bg=BG_MID, bd=1, relief=tk.SOLID)
        cf.pack(fill=tk.BOTH, expand=True)
        self._canvas = tk.Canvas(cf, bg=BG_DARK, highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.bind("<Configure>", lambda _: self._render_frame())
        self._hint_lbl = tk.Label(
            center,
            text="Preview shows the selected video stretched to the target size",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 9))
        self._hint_lbl.pack(pady=(4, 0))

        # ── Right: controls ───────────────────────────────────────────────────
        rf = tk.Frame(body, bg=BG_DARK, width=290)
        rf.pack(side=tk.RIGHT, fill=tk.Y)
        rf.pack_propagate(False)
        tk.Label(rf, text="Stretch / Resize", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(14, 2))
        tk.Label(rf, text="Force every video to one exact resolution.\n"
                          "Aspect ratio is ignored, so squished footage is\n"
                          "stretched back into shape.",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9),
                 wraplength=270, justify=tk.LEFT).pack(padx=8, pady=(0, 6))
        _separator(rf)

        # Preset picker
        pr = tk.Frame(rf, bg=BG_DARK)
        pr.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(pr, text="Preset:", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._preset_var = tk.StringVar(value="Custom")
        ttk.Combobox(pr, textvariable=self._preset_var,
                     values=list(STRETCH_PRESETS.keys()), state="readonly",
                     font=("Segoe UI", 9)).pack(fill=tk.X, pady=(2, 0))
        self._preset_var.trace_add("write", self._on_preset)

        # Width / height
        dim = tk.Frame(rf, bg=BG_DARK)
        dim.pack(fill=tk.X, padx=10, pady=(8, 2))
        tk.Label(dim, text="Target resolution (W × H):", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        dinner = tk.Frame(dim, bg=BG_DARK)
        dinner.pack(fill=tk.X, pady=(2, 0))
        self._w_entry = tk.Entry(dinner, width=7, bg=BG_MID, fg=FG,
                                 insertbackground=FG, relief=tk.FLAT,
                                 font=("Segoe UI", 10))
        self._w_entry.pack(side=tk.LEFT)
        self._w_entry.insert(0, "1920")
        tk.Label(dinner, text="  ×  ", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._h_entry = tk.Entry(dinner, width=7, bg=BG_MID, fg=FG,
                                 insertbackground=FG, relief=tk.FLAT,
                                 font=("Segoe UI", 10))
        self._h_entry.pack(side=tk.LEFT)
        self._h_entry.insert(0, "1080")
        for e in (self._w_entry, self._h_entry):
            e.bind("<KeyRelease>", lambda _: self._render_frame())

        _separator(rf)
        fmt_row = tk.Frame(rf, bg=BG_DARK)
        fmt_row.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(fmt_row, text="Output Format:", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._fmt_var = tk.StringVar(value=CROP_FORMAT_NAMES[0])
        ttk.Combobox(fmt_row, textvariable=self._fmt_var,
                     values=CROP_FORMAT_NAMES, state="readonly",
                     font=("Segoe UI", 9)).pack(fill=tk.X, pady=(2, 0))

        _separator(rf)
        self._progress = tk.DoubleVar(value=0)
        ttk.Progressbar(rf, variable=self._progress, maximum=100,
                        style="Stretch.Horizontal.TProgressbar", length=270
                        ).pack(padx=10, pady=(8, 3))
        self._stretch_btn = tk.Button(
            rf, text="STRETCH ALL", command=self._start_stretch_thread,
            bg=GREEN, fg=BG_DARK, font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT, pady=9)
        self._stretch_btn.pack(fill=tk.X, padx=10, pady=(3, 4))
        self._status = tk.StringVar(
            value="Add videos, set a resolution, click Stretch All.")
        tk.Label(rf, textvariable=self._status,
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9),
                 wraplength=270, justify=tk.LEFT
                 ).pack(padx=10, anchor="w", pady=(0, 8))

        self.root.after(150, self._draw_placeholder)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _on_preset(self, *_) -> None:
        dims = STRETCH_PRESETS.get(self._preset_var.get())
        if dims:
            for entry, val in ((self._w_entry, dims[0]), (self._h_entry, dims[1])):
                entry.delete(0, tk.END)
                entry.insert(0, str(val))
            self._render_frame()

    def _target_dims(self):
        """Return (w, h) as even positive ints within range, or None if invalid."""
        try:
            w = int(float(self._w_entry.get().strip()))
            h = int(float(self._h_entry.get().strip()))
        except (ValueError, AttributeError):
            return None
        if not (2 <= w <= 7680 and 2 <= h <= 7680):
            return None
        return (w - (w % 2), h - (h % 2))      # ffmpeg yuv420p needs even dims

    def _draw_placeholder(self) -> None:
        self._canvas.delete("placeholder")
        w, h = self._canvas.winfo_width(), self._canvas.winfo_height()
        self._canvas.create_text(
            w // 2, h // 2, text="Add videos to preview the stretch",
            fill="#45475a", font=("Segoe UI", 14), tags="placeholder",
            justify=tk.CENTER, width=max(w - 40, 100))

    # ── File management ───────────────────────────────────────────────────────

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select video files", filetypes=VIDEO_FILETYPES)
        if not paths:
            return
        existing = {f["path"] for f in self._files}
        added = 0
        for p in paths:
            if p in existing:
                continue
            cap = cv2.VideoCapture(p)
            if not cap.isOpened():
                cap.release()
                continue
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if total > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
            ok, bgr = cap.read()
            frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB) if ok else None
            cap.release()
            if w <= 0 or h <= 0:
                continue
            self._files.append({
                "path": p, "w": w, "h": h, "total": total, "fps": fps,
                "frame": frame,
            })
            added += 1
        if not added:
            return
        self._refresh_list()
        self._bar_lbl.config(text=f"{len(self._files)} file(s) loaded")
        self._status.set(f"Added {added} file(s).")
        if self._cur_idx == -1 and self._files:
            self._cur_idx = 0
            self._tree.selection_set("0")
            self._tree.focus("0")
            self._render_frame()

    def _refresh_list(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for i, f in enumerate(self._files):
            self._tree.insert("", "end", iid=str(i),
                              text=os.path.basename(f["path"]),
                              values=(_folder_of(f["path"]), f"{f['w']}×{f['h']}"))
        if self._cur_idx >= 0:
            self._tree.selection_set(str(self._cur_idx))
            self._tree.focus(str(self._cur_idx))

    def _remove_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        cur_path = self._files[self._cur_idx]["path"] if self._cur_idx >= 0 else None
        for idx in sorted((int(i) for i in sel), reverse=True):
            self._files.pop(idx)
        self._cur_idx = next((i for i, f in enumerate(self._files)
                              if f["path"] == cur_path), -1) if cur_path else -1
        if self._cur_idx < 0:
            self._photo = None
            self._canvas.delete("all")
            self._draw_placeholder()
        self._refresh_list()
        self._bar_lbl.config(
            text=f"{len(self._files)} file(s) loaded" if self._files else "No files loaded")

    def _on_file_select(self, _=None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx == self._cur_idx:
            return
        self._cur_idx = idx
        self._render_frame()

    # ── Preview (source frame distorted to the target aspect) ─────────────────

    def _render_frame(self) -> None:
        if self._cur_idx < 0 or self._cur_idx >= len(self._files):
            self._draw_placeholder()
            return
        entry = self._files[self._cur_idx]
        frame = entry["frame"]
        if frame is None:
            self._canvas.delete("all")
            self._draw_placeholder()
            return
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        if cw < 4 or ch < 4:
            self.root.after(60, self._render_frame)
            return
        dims = self._target_dims()
        tw, th = dims if dims else (frame.shape[1], frame.shape[0])
        # Fit the *target* rectangle inside the canvas, then stretch the source
        # frame to fill it — that visually shows the distortion that will result.
        ratio = min(cw / tw, ch / th)
        nw, nh = max(1, int(tw * ratio)), max(1, int(th * ratio))
        tile = Image.fromarray(frame).resize((nw, nh), Image.LANCZOS)
        composite = Image.new("RGB", (cw, ch), (0x11, 0x11, 0x1b))
        composite.paste(tile, ((cw - nw) // 2, (ch - nh) // 2))
        self._photo = ImageTk.PhotoImage(composite)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)

        label = (f"{entry['w']}×{entry['h']}  →  {tw}×{th}"
                 if dims else f"{entry['w']}×{entry['h']}  →  (enter a valid size)")
        self._canvas.create_rectangle(
            cw // 2 - 150, ch - 34, cw // 2 + 150, ch - 8,
            fill="#000000", stipple="gray50", outline="")
        self._canvas.create_text(
            cw // 2, ch - 20, text=label, fill="white",
            font=("Segoe UI", 10, "bold"))

    # ── Stretch (parallel ffmpeg) ─────────────────────────────────────────────

    def _start_stretch_thread(self) -> None:
        if self._running:
            return
        if not self._files:
            messagebox.showwarning("No Files", "Add at least one video file first.")
            return
        if not _ffmpeg_available():
            messagebox.showwarning(
                "ffmpeg Missing",
                "ffmpeg must be installed and on PATH to stretch videos.")
            return
        dims = self._target_dims()
        if not dims:
            messagebox.showwarning(
                "Invalid Resolution",
                "Enter a valid width and height (2–7680). Odd values are\n"
                "rounded down to the nearest even number.")
            return
        tw, th = dims
        fmt_key = self._fmt_var.get()
        jobs = [f["path"] for f in self._files]
        self._running = True
        self._stretch_btn.config(state=tk.DISABLED)
        self._progress.set(0)
        self._status.set(f"Stretching {len(jobs)} file(s) → {tw}×{th}…")
        threading.Thread(
            target=self._do_stretch, args=(jobs, tw, th, fmt_key),
            daemon=True).start()

    def _do_stretch(self, jobs: list, tw: int, th: int, fmt_key: str) -> None:
        fmt_args, out_ext = OUTPUT_FORMATS.get(fmt_key, list(OUTPUT_FORMATS.values())[0])
        n = len(jobs)
        done_count = [0]
        errors = []
        lock = threading.Lock()
        vf = f"scale={tw}:{th},setsar=1"

        def _process_one(src: str):
            src_dir = os.path.dirname(os.path.abspath(src))
            out_dir = os.path.join(src_dir, "stretched")
            os.makedirs(out_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(src))[0]
            out_path = os.path.join(out_dir, base + out_ext)
            try:
                _run_ffmpeg(
                    ["-hwaccel", "auto", "-i", src, "-vf", vf]
                    + fmt_args + [out_path])
                return None
            except RuntimeError as exc:
                return f"{os.path.basename(src)}: {exc}"

        max_workers = min(4, n)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as exe:
            futures = {exe.submit(_process_one, src): src for src in jobs}
            for fut in concurrent.futures.as_completed(futures):
                err = fut.result()
                with lock:
                    if err:
                        errors.append(err)
                    done_count[0] += 1
                self.root.after(0, self._progress.set, done_count[0] / n * 100)

        def _finish() -> None:
            self._running = False
            self._stretch_btn.config(state=tk.NORMAL)
            self._progress.set(100)
            _play_cash_register()
            if errors:
                messagebox.showerror(
                    "Stretch Errors",
                    f"{len(errors)} file(s) failed:\n\n"
                    + "\n".join(e[:200] for e in errors[:5]))
                self._status.set(
                    f"Done with {len(errors)} error(s).  "
                    f"{n - len(errors)} file(s) succeeded.")
            else:
                self._status.set(
                    f"Done!  {n} file(s) saved to 'stretched' folder(s).")
                messagebox.showinfo(
                    "Stretch Complete",
                    f"{n} video(s) stretched to {tw}×{th}.\n"
                    "Output saved to a 'stretched' folder next to each source video.")

        self.root.after(0, _finish)


# ════════════════════════════════════════════════════════════════════════════
# Convert Tab  (batch re-encode legacy formats — e.g. MPG — to MP4)
# ════════════════════════════════════════════════════════════════════════════

CONVERT_FILETYPES = [
    ("MPEG video", "*.mpg *.mpeg *.m1v *.m2v *.vob"),
    ("Video files", "*.mp4 *.avi *.mov *.mkv *.mpg *.mpeg *.wmv *.flv *.webm *.m4v"),
    ("All files", "*.*"),
]


class ConvertTab:
    """
    Batch re-encode videos — typically legacy MPG/MPEG — into modern MP4.
    Each file is converted with the chosen output format into a 'converted'
    folder next to the source. Up to 4 ffmpeg processes run concurrently.
    """

    def __init__(self, parent: tk.Frame, root: tk.Tk) -> None:
        self.parent = parent
        self.root = root
        self._files: list = []      # {path, w, h, ext}
        self._running = False
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        bar = tk.Frame(self.parent, bg=BG_DARK, pady=7)
        bar.pack(fill=tk.X)
        tk.Button(bar, text="  Add Videos  ", command=self._add_files,
                  bg=ACCENT, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=10)
        tk.Button(bar, text="  Remove Selected  ", command=self._remove_selected,
                  bg=RED, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=2)
        self._bar_lbl = tk.Label(bar, text="No files loaded",
                                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 10))
        self._bar_lbl.pack(side=tk.LEFT, padx=10)

        body = tk.Frame(self.parent, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # ── Left: file queue ──────────────────────────────────────────────────
        lf = tk.Frame(body, bg=BG_DARK, width=320)
        lf.pack(side=tk.LEFT, fill=tk.Y)
        lf.pack_propagate(False)
        tk.Label(lf, text="Video Queue", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(12, 6))
        lb_wrap = tk.Frame(lf, bg=BG_DARK)
        lb_wrap.pack(fill=tk.BOTH, expand=True, padx=6)
        lb_sb = tk.Scrollbar(lb_wrap, orient=tk.VERTICAL)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree = _build_queue_tree(lb_wrap, lb_sb)
        tk.Label(lf, text="Ctrl/Shift-click to select multiple, then Remove Selected.",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 8),
                 wraplength=300, justify=tk.LEFT).pack(padx=8, pady=(4, 2), anchor="w")

        # ── Center: conversion queue summary ──────────────────────────────────
        center = tk.Frame(body, bg=BG)
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8)
        tk.Label(center, text="Conversion Queue", bg=BG, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(8, 4))
        txt_wrap = tk.Frame(center, bg=BG_MID, bd=1, relief=tk.SOLID)
        txt_wrap.pack(fill=tk.BOTH, expand=True)
        txt_sb = tk.Scrollbar(txt_wrap, orient=tk.VERTICAL)
        txt_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._queue_text = tk.Text(
            txt_wrap, yscrollcommand=txt_sb.set,
            bg=BG_DARK, fg=FG, font=("Consolas", 9),
            relief=tk.FLAT, state=tk.DISABLED, wrap=tk.NONE,
            highlightthickness=0, borderwidth=0, padx=8, pady=8)
        self._queue_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        txt_sb.config(command=self._queue_text.yview)

        # ── Right: controls ───────────────────────────────────────────────────
        rf = tk.Frame(body, bg=BG_DARK, width=290)
        rf.pack(side=tk.RIGHT, fill=tk.Y)
        rf.pack_propagate(False)
        tk.Label(rf, text="Convert to MP4", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(14, 2))
        tk.Label(rf, text="Re-encode legacy videos (e.g. MPG/MPEG)\n"
                          "into modern, widely-compatible MP4 files.",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9),
                 wraplength=270, justify=tk.LEFT).pack(padx=8, pady=(0, 6))
        _separator(rf)

        fmt_row = tk.Frame(rf, bg=BG_DARK)
        fmt_row.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(fmt_row, text="Output Format:", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._fmt_var = tk.StringVar(value=CROP_FORMAT_NAMES[0])
        ttk.Combobox(fmt_row, textvariable=self._fmt_var,
                     values=CROP_FORMAT_NAMES, state="readonly",
                     font=("Segoe UI", 9)).pack(fill=tk.X, pady=(2, 0))

        _separator(rf)
        self._progress = tk.DoubleVar(value=0)
        ttk.Progressbar(rf, variable=self._progress, maximum=100,
                        style="Convert.Horizontal.TProgressbar", length=270
                        ).pack(padx=10, pady=(8, 3))
        self._convert_btn = tk.Button(
            rf, text="CONVERT ALL", command=self._start_convert_thread,
            bg=GREEN, fg=BG_DARK, font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT, pady=9)
        self._convert_btn.pack(fill=tk.X, padx=10, pady=(3, 4))
        self._status = tk.StringVar(value="Add videos and click Convert All.")
        tk.Label(rf, textvariable=self._status,
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9),
                 wraplength=270, justify=tk.LEFT
                 ).pack(padx=10, anchor="w", pady=(0, 8))

    # ── File management ───────────────────────────────────────────────────────

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select videos to convert", filetypes=CONVERT_FILETYPES)
        if not paths:
            return
        existing = {f["path"] for f in self._files}
        added = 0
        for p in paths:
            if p in existing:
                continue
            cap = cv2.VideoCapture(p)
            opened = cap.isOpened()
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()
            if not opened:
                continue
            self._files.append({
                "path": p, "w": w, "h": h,
                "ext": os.path.splitext(p)[1].lower(),
            })
            added += 1
        if not added:
            return
        self._refresh_list()
        self._bar_lbl.config(text=f"{len(self._files)} file(s) loaded")
        self._status.set(f"Added {added} file(s). Click Convert All.")
        self._refresh_queue()

    def _refresh_list(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for i, f in enumerate(self._files):
            self._tree.insert("", "end", iid=str(i),
                              text=os.path.basename(f["path"]),
                              values=(_folder_of(f["path"]),))

    def _remove_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        for idx in sorted((int(i) for i in sel), reverse=True):
            self._files.pop(idx)
        self._refresh_list()
        self._bar_lbl.config(
            text=f"{len(self._files)} file(s) loaded" if self._files else "No files loaded")
        self._refresh_queue()

    def _refresh_queue(self) -> None:
        _, out_ext = OUTPUT_FORMATS.get(
            self._fmt_var.get(), list(OUTPUT_FORMATS.values())[0])
        self._queue_text.config(state=tk.NORMAL)
        self._queue_text.delete("1.0", tk.END)
        for i, f in enumerate(self._files, 1):
            name = os.path.basename(f["path"])
            res = f"{f['w']}×{f['h']}" if f["w"] > 0 else "?"
            self._queue_text.insert(
                tk.END, f"{i:>2}. {name}\n     {res}   {f['ext']} → {out_ext}\n\n")
        self._queue_text.config(state=tk.DISABLED)

    # ── Conversion (parallel ffmpeg) ──────────────────────────────────────────

    def _start_convert_thread(self) -> None:
        if self._running:
            return
        if not self._files:
            messagebox.showwarning("No Files", "Add at least one video file first.")
            return
        if not _ffmpeg_available():
            messagebox.showwarning(
                "ffmpeg Missing",
                "ffmpeg must be installed and on PATH to convert videos.")
            return
        fmt_key = self._fmt_var.get()
        jobs = [f["path"] for f in self._files]
        self._running = True
        self._convert_btn.config(state=tk.DISABLED)
        self._progress.set(0)
        self._status.set(f"Converting {len(jobs)} file(s) to MP4…")
        threading.Thread(
            target=self._do_convert, args=(jobs, fmt_key), daemon=True).start()

    def _do_convert(self, jobs: list, fmt_key: str) -> None:
        fmt_args, out_ext = OUTPUT_FORMATS.get(fmt_key, list(OUTPUT_FORMATS.values())[0])
        n = len(jobs)
        done_count = [0]
        errors = []
        lock = threading.Lock()

        def _process_one(src: str):
            src_dir = os.path.dirname(os.path.abspath(src))
            out_dir = os.path.join(src_dir, "converted")
            os.makedirs(out_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(src))[0]
            out_path = os.path.join(out_dir, base + out_ext)
            try:
                _run_ffmpeg(["-hwaccel", "auto", "-i", src] + fmt_args + [out_path])
                return None
            except RuntimeError as exc:
                return f"{os.path.basename(src)}: {exc}"

        max_workers = min(4, n)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as exe:
            futures = {exe.submit(_process_one, src): src for src in jobs}
            for fut in concurrent.futures.as_completed(futures):
                err = fut.result()
                with lock:
                    if err:
                        errors.append(err)
                    done_count[0] += 1
                self.root.after(0, self._progress.set, done_count[0] / n * 100)

        def _finish() -> None:
            self._running = False
            self._convert_btn.config(state=tk.NORMAL)
            self._progress.set(100)
            _play_cash_register()
            if errors:
                messagebox.showerror(
                    "Conversion Errors",
                    f"{len(errors)} file(s) failed:\n\n"
                    + "\n".join(e[:200] for e in errors[:5]))
                self._status.set(
                    f"Done with {len(errors)} error(s).  "
                    f"{n - len(errors)} file(s) succeeded.")
            else:
                self._status.set(
                    f"Done!  {n} file(s) saved to 'converted' folder(s).")
                messagebox.showinfo(
                    "Conversion Complete",
                    f"{n} video(s) converted to MP4.\n"
                    "Output saved to a 'converted' folder next to each source video.")

        self.root.after(0, _finish)


# ════════════════════════════════════════════════════════════════════════════
# Split Tab  (halve each video down the middle + intelligent file naming)
# ════════════════════════════════════════════════════════════════════════════

class SplitTab:
    """
    Split a batch of videos down the exact vertical centre into a left and a
    right half, naming each output by parsing the source filename.

    Naming rule (auto): the trailing run of digits is split in half and each
    half is re-joined to the non-numeric prefix, e.g.

        CAB0108  →  CAB01 (left half)  +  CAB08 (right half)
        CAB1112  →  CAB11 (left half)  +  CAB12 (right half)

    Files whose trailing digits can't be split evenly fall back to a plain
    '_left' / '_right' suffix (shown in the parse preview). Output is written
    to a 'split' folder next to each source video.
    """

    def __init__(self, parent: tk.Frame, root: tk.Tk) -> None:
        self.parent = parent
        self.root = root
        self._files: list = []      # {path, w, h}
        self._running = False
        self._build_ui()

    # ── Filename parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_name(base: str):
        """Return (left_name, right_name, ok).

        The run of digits encoding the two side-by-side subjects is split in
        half, while any '_session' suffix that follows an underscore is kept
        on both halves.  e.g. 'DR2324_Acquisition' → 'DR23_Acquisition' /
        'DR24_Acquisition', and 'CAB0102' → 'CAB01' / 'CAB02'.

        ok is True when the digits split evenly into two distinct halves;
        otherwise names fall back to '<base>_left' / '<base>_right'.
        """
        # Separate the subject token from an optional '_session' suffix so an
        # underscore in the name (e.g. the session label) doesn't defeat the
        # trailing-digit match below.
        token, sep, rest = base.partition("_")
        suffix = sep + rest  # "" when there's no underscore, else "_<session>"

        m = re.search(r"(\d+)$", token)
        if m:
            digits = m.group(1)
            if len(digits) >= 2 and len(digits) % 2 == 0:
                half = len(digits) // 2
                prefix = token[:m.start()]
                left = prefix + digits[:half] + suffix
                right = prefix + digits[half:] + suffix
                # Guard against identical halves (e.g. CAB0101 → CAB01/CAB01),
                # which would make the right output overwrite the left.
                if left != right:
                    return left, right, True
        return base + "_left", base + "_right", False

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Dark-themed Treeview for the parse preview
        style = ttk.Style()
        style.configure("Split.Treeview", background=BG_MID, foreground=FG,
                        fieldbackground=BG_MID, font=("Segoe UI", 9),
                        rowheight=23, borderwidth=0)
        style.configure("Split.Treeview.Heading", background=BG_DARK,
                        foreground=ACCENT, font=("Segoe UI", 9, "bold"),
                        relief=tk.FLAT)
        style.map("Split.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", BG_DARK)])

        bar = tk.Frame(self.parent, bg=BG_DARK, pady=7)
        bar.pack(fill=tk.X)
        tk.Button(bar, text="  Add Videos  ", command=self._add_files,
                  bg=ACCENT, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=10)
        tk.Button(bar, text="  Remove Selected  ", command=self._remove_selected,
                  bg=RED, fg=BG_DARK, font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=2)
        self._bar_lbl = tk.Label(bar, text="No files loaded",
                                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 10))
        self._bar_lbl.pack(side=tk.LEFT, padx=10)

        body = tk.Frame(self.parent, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # ── Center: name-parse preview table ──────────────────────────────────
        center = tk.Frame(body, bg=BG)
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))
        tk.Label(center, text="Name Parse Preview", bg=BG, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(4, 4))

        tv_wrap = tk.Frame(center, bg=BG_MID, bd=1, relief=tk.SOLID)
        tv_wrap.pack(fill=tk.BOTH, expand=True)
        tv_sb = tk.Scrollbar(tv_wrap, orient=tk.VERTICAL)
        tv_sb.pack(side=tk.RIGHT, fill=tk.Y)
        cols = ("folder", "left", "right", "status")
        self._tree = ttk.Treeview(
            tv_wrap, columns=cols, show="tree headings",
            style="Split.Treeview", yscrollcommand=tv_sb.set, selectmode="extended")
        self._tree.heading("#0", text="Source file")
        self._tree.heading("folder", text="Folder")
        self._tree.heading("left", text="Left half  →")
        self._tree.heading("right", text="Right half  →")
        self._tree.heading("status", text="Parse")
        self._tree.column("#0", width=180, anchor="w", stretch=True)
        self._tree.column("folder", width=190, anchor="w", stretch=True)
        self._tree.column("left", width=130, anchor="w")
        self._tree.column("right", width=130, anchor="w")
        self._tree.column("status", width=100, anchor="center", stretch=False)
        self._tree.tag_configure("warn", foreground=YELLOW)
        self._tree.tag_configure("edited", foreground=ACCENT)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tv_sb.config(command=self._tree.yview)
        # Double-click a Left/Right cell to edit the output name in place.
        self._tree.bind("<Double-1>", self._on_cell_double_click)

        tk.Label(center,
                 text="Each video is cut down the exact vertical middle: "
                      "left half → first name, right half → second name.  "
                      "Double-click a Left/Right name to edit it; "
                      "Ctrl/Shift-click to select multiple for removal.",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
                 wraplength=620, justify=tk.LEFT).pack(pady=(4, 0), anchor="w")

        # ── Right: controls ───────────────────────────────────────────────────
        rf = tk.Frame(body, bg=BG_DARK, width=290)
        rf.pack(side=tk.RIGHT, fill=tk.Y)
        rf.pack_propagate(False)
        tk.Label(rf, text="Split Down the Middle", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(pady=(14, 2))
        tk.Label(rf, text="Halve each video vertically and name the two\n"
                          "outputs by parsing the source filename.\n\n"
                          "e.g.  CAB0108  →  CAB01  +  CAB08",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9),
                 wraplength=270, justify=tk.LEFT).pack(padx=8, pady=(0, 6))
        _separator(rf)

        fmt_row = tk.Frame(rf, bg=BG_DARK)
        fmt_row.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(fmt_row, text="Output Format:", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._fmt_var = tk.StringVar(value=CROP_FORMAT_NAMES[0])
        ttk.Combobox(fmt_row, textvariable=self._fmt_var,
                     values=CROP_FORMAT_NAMES, state="readonly",
                     font=("Segoe UI", 9)).pack(fill=tk.X, pady=(2, 0))
        self._fmt_var.trace_add("write", lambda *_: self._refresh_preview())

        # Audio is dropped by default (vertical split is video-only work)
        self._audio_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            rf, text="Include audio in both halves", variable=self._audio_var,
            bg=BG_DARK, fg=FG, selectcolor=BG_MID,
            activebackground=BG_DARK, activeforeground=FG,
            font=("Segoe UI", 9)).pack(anchor="w", padx=10, pady=(6, 0))

        # Flip the extracted subject order: the physical left/right halves take
        # the opposite subject name (e.g. CAB0102 → CAB02 + CAB01).
        self._flip_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            rf, text="Flip subject order  (e.g. CAB02 + CAB01)",
            variable=self._flip_var, command=self._on_flip_toggle,
            bg=BG_DARK, fg=FG, selectcolor=BG_MID,
            activebackground=BG_DARK, activeforeground=FG,
            font=("Segoe UI", 9)).pack(anchor="w", padx=10, pady=(6, 0))

        _separator(rf)
        self._progress = tk.DoubleVar(value=0)
        ttk.Progressbar(rf, variable=self._progress, maximum=100,
                        style="Split.Horizontal.TProgressbar", length=270
                        ).pack(padx=10, pady=(8, 3))
        self._split_btn = tk.Button(
            rf, text="SPLIT ALL", command=self._start_split_thread,
            bg=GREEN, fg=BG_DARK, font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT, pady=9)
        self._split_btn.pack(fill=tk.X, padx=10, pady=(3, 4))
        self._status = tk.StringVar(value="Add videos to preview the split names.")
        tk.Label(rf, textvariable=self._status,
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 9),
                 wraplength=270, justify=tk.LEFT
                 ).pack(padx=10, anchor="w", pady=(0, 8))

    # ── File management ───────────────────────────────────────────────────────

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select video files", filetypes=VIDEO_FILETYPES)
        if not paths:
            return
        existing = {f["path"] for f in self._files}
        added = 0
        for p in paths:
            if p in existing:
                continue
            cap = cv2.VideoCapture(p)
            opened = cap.isOpened()
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()
            if not opened:
                continue
            base = os.path.splitext(os.path.basename(p))[0]
            left, right, ok = self._parse_name(base)
            # Honour the current flip state so files added while flipping is on
            # match the rest of the list.
            if self._flip_var.get():
                left, right = right, left
            self._files.append({"path": p, "w": w, "h": h,
                                 "left": left, "right": right,
                                 "ok": ok, "edited": False})
            added += 1
        if not added:
            return
        self._bar_lbl.config(text=f"{len(self._files)} file(s) loaded")
        self._status.set(f"Added {added} file(s). Review the parse preview, then Split All.")
        self._refresh_preview()

    def _remove_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        for idx in sorted((int(i) for i in sel), reverse=True):
            if idx < len(self._files):
                self._files.pop(idx)
        self._bar_lbl.config(
            text=f"{len(self._files)} file(s) loaded" if self._files else "No files loaded")
        self._refresh_preview()

    def _on_flip_toggle(self) -> None:
        """Swap the two output names for every loaded file so the physical
        left/right halves take the opposite subject (e.g. CAB01 + CAB02 →
        CAB02 + CAB01).  Toggling again restores the original order."""
        for f in self._files:
            f["left"], f["right"] = f["right"], f["left"]
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        _, out_ext = OUTPUT_FORMATS.get(
            self._fmt_var.get(), list(OUTPUT_FORMATS.values())[0])
        self._tree.delete(*self._tree.get_children())
        for i, f in enumerate(self._files):
            if f.get("edited"):
                status, tags = "edited", ("edited",)
            elif f["ok"]:
                status, tags = "OK", ()
            else:
                status, tags = "auto _left/_right", ("warn",)
            self._tree.insert(
                "", "end", iid=str(i), text=os.path.basename(f["path"]),
                values=(_folder_of(f["path"]),
                        f["left"] + out_ext, f["right"] + out_ext, status),
                tags=tags)

    # ── Inline editing of output names ─────────────────────────────────────────

    def _on_cell_double_click(self, event) -> None:
        """Double-clicking the Left/Right column opens an in-place name editor."""
        if self._running:
            return
        if self._tree.identify_region(event.x, event.y) != "cell":
            return
        col = self._tree.identify_column(event.x)   # "#1".."#4"
        row = self._tree.identify_row(event.y)
        key = {"#2": "left", "#3": "right"}.get(col)
        if not row or key is None:
            return
        bbox = self._tree.bbox(row, col)
        if not bbox:
            return
        x, y, w, h = bbox
        idx = int(row)
        edit = tk.Entry(self._tree, bg=BG_DARK, fg=FG, insertbackground=FG,
                        relief=tk.SOLID, font=("Segoe UI", 9))
        edit.insert(0, self._files[idx][key])
        edit.select_range(0, tk.END)
        edit.focus_set()
        edit.place(x=x, y=y, width=w, height=h)

        def commit(_=None):
            if not edit.winfo_exists():
                return
            new = edit.get().strip()
            edit.destroy()
            if idx >= len(self._files):
                return
            # Drop any path separators / characters Windows forbids in names,
            # and a trailing extension the user may have typed by habit.
            new = re.sub(r'[\\/:*?"<>|]', "", new).strip()
            _, out_ext = OUTPUT_FORMATS.get(
                self._fmt_var.get(), list(OUTPUT_FORMATS.values())[0])
            if new.lower().endswith(out_ext.lower()):
                new = new[: -len(out_ext)].strip()
            if new:
                self._files[idx][key] = new
                self._files[idx]["edited"] = True
            self._refresh_preview()

        def cancel(_=None):
            if edit.winfo_exists():
                edit.destroy()

        edit.bind("<Return>", commit)
        edit.bind("<FocusOut>", commit)
        edit.bind("<Escape>", cancel)

    # ── Split (parallel ffmpeg) ───────────────────────────────────────────────

    def _start_split_thread(self) -> None:
        if self._running:
            return
        if not self._files:
            messagebox.showwarning("No Files", "Add at least one video file first.")
            return
        if not _ffmpeg_available():
            messagebox.showwarning(
                "ffmpeg Missing",
                "ffmpeg must be installed and on PATH to split videos.")
            return
        fmt_key = self._fmt_var.get()
        include_audio = self._audio_var.get()
        jobs = [(f["path"], f["left"], f["right"]) for f in self._files]
        self._running = True
        self._split_btn.config(state=tk.DISABLED)
        self._progress.set(0)
        self._status.set(f"Splitting {len(jobs)} file(s) down the middle…")
        threading.Thread(
            target=self._do_split, args=(jobs, fmt_key, include_audio),
            daemon=True).start()

    def _do_split(self, jobs: list, fmt_key: str, include_audio: bool) -> None:
        fmt_args, out_ext = OUTPUT_FORMATS.get(fmt_key, list(OUTPUT_FORMATS.values())[0])
        n = len(jobs)
        done_count = [0]
        errors = []
        lock = threading.Lock()

        # Drop audio unless the user opted in (a vertical split is video work).
        audio_args = [] if include_audio else ["-an"]

        # CPU fallback encoder (first non-NVENC format, e.g. H.264 ultrafast).
        # Used automatically when a GPU/NVENC encode fails — e.g. the NVIDIA
        # driver is older than the ffmpeg build's required NVENC API version.
        nvenc = any("nvenc" in a for a in fmt_args)
        cpu_fmt_args, cpu_ext = next(
            ((a, e) for a, e in OUTPUT_FORMATS.values()
             if not any("nvenc" in x for x in a)),
            (fmt_args, out_ext))
        fallbacks = [0]

        # Crop width/height are rounded down to even numbers: NVENC (and
        # yuv420p) require even dimensions, and a plain iw/2 is odd whenever
        # half the source width is odd. The cut stays at the exact middle
        # (x=iw/2); at most one edge column is dropped.
        ew = "trunc(iw/4)*2"      # even half-width
        eh = "trunc(ih/2)*2"      # even height

        def _split_with(src, out_dir, left_name, right_name, vargs, ext):
            # Two simple -vf crop passes (one per half) — the same robust
            # single-filter form every other tab uses.
            _run_ffmpeg(
                ["-hwaccel", "auto", "-i", src, "-vf", f"crop={ew}:{eh}:0:0"]
                + vargs + audio_args + [os.path.join(out_dir, left_name + ext)])
            _run_ffmpeg(
                ["-hwaccel", "auto", "-i", src, "-vf", f"crop={ew}:{eh}:iw/2:0"]
                + vargs + audio_args + [os.path.join(out_dir, right_name + ext)])

        def _process_one(job):
            src, left_name, right_name = job
            src_dir = os.path.dirname(os.path.abspath(src))
            out_dir = os.path.join(src_dir, "split")
            os.makedirs(out_dir, exist_ok=True)
            try:
                _split_with(src, out_dir, left_name, right_name, fmt_args, out_ext)
                return None
            except RuntimeError as exc:
                if not nvenc:
                    return f"{os.path.basename(src)}: {exc}"
                # GPU encode failed — retry this file on the CPU encoder so it
                # still completes (e.g. NVIDIA driver too old for NVENC).
                try:
                    _split_with(src, out_dir, left_name, right_name,
                                cpu_fmt_args, cpu_ext)
                    with lock:
                        fallbacks[0] += 1
                    return None
                except RuntimeError as exc2:
                    return f"{os.path.basename(src)}: {exc2}"

        max_workers = min(4, n)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as exe:
            futures = {exe.submit(_process_one, job): job for job in jobs}
            for fut in concurrent.futures.as_completed(futures):
                err = fut.result()
                with lock:
                    if err:
                        errors.append(err)
                    done_count[0] += 1
                self.root.after(0, self._progress.set, done_count[0] / n * 100)

        def _finish() -> None:
            self._running = False
            self._split_btn.config(state=tk.NORMAL)
            self._progress.set(100)
            _play_cash_register()
            if errors:
                messagebox.showerror(
                    "Split Errors",
                    f"{len(errors)} file(s) failed:\n\n"
                    + "\n".join(e[:200] for e in errors[:5]))
                self._status.set(
                    f"Done with {len(errors)} error(s).  "
                    f"{n - len(errors)} file(s) succeeded.")
            else:
                self._status.set(
                    f"Done!  {n} file(s) split into 'split' folder(s).")
                note = ""
                if fallbacks[0]:
                    note = (f"\n\nNote: {fallbacks[0]} file(s) fell back to CPU "
                            "encoding because GPU/NVENC failed (your NVIDIA "
                            "driver is older than this ffmpeg build requires — "
                            "update to driver 570+ to use the GPU).")
                messagebox.showinfo(
                    "Split Complete",
                    f"{n} video(s) split down the middle.\n"
                    "Left/right halves saved to a 'split' folder next to each source."
                    + note)

        self.root.after(0, _finish)


# ════════════════════════════════════════════════════════════════════════════
# Update engine  (git-based, checked against the GitHub remote)
# ════════════════════════════════════════════════════════════════════════════

GITHUB_REPO = "JobeRitchie/KashCropper"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"
UPDATE_BRANCH = "main"

# Stop git from flashing a console window on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _git(args, timeout: int = 30):
    """Run git inside the app folder; return (returncode, combined output).

    Never raises — a missing git binary, a timeout, or any other failure comes
    back as a non-zero code with the reason as the text, so every caller can
    just report it instead of guarding each call.
    """
    try:
        proc = subprocess.run(
            ["git"] + list(args), cwd=_SCRIPT_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return proc.returncode, proc.stdout.strip()
    except FileNotFoundError:
        return 127, ("git was not found — install it from "
                     "https://git-scm.com/downloads, then restart the app")
    except subprocess.TimeoutExpired:
        return 124, "git timed out — check your network connection"
    except Exception as exc:
        return 1, str(exc)


def _is_git_checkout() -> bool:
    """True if the app folder is a git clone, i.e. the updater can work here."""
    rc, out = _git(["rev-parse", "--is-inside-work-tree"], timeout=10)
    return rc == 0 and out == "true"


def _ensure_git_safe_directory() -> None:
    """Register the app folder as a git `safe.directory` when it is owned by a
    different account (common on network shares), where git otherwise refuses
    every command with a 'dubious ownership' error."""
    rc, out = _git(["status", "--porcelain"], timeout=10)
    if rc == 0 or "dubious ownership" not in out:
        return
    # UNC paths get recorded with either separator, so register both forms.
    for path in {_SCRIPT_DIR, _SCRIPT_DIR.replace("\\", "/")}:
        _git(["config", "--global", "--add", "safe.directory", path], timeout=10)


def _local_revision() -> str:
    """Short commit hash of the installed copy, or '' if not a git checkout."""
    rc, out = _git(["rev-parse", "--short", "HEAD"], timeout=10)
    return out if rc == 0 else ""


def _check_for_update(timeout: int = 30):
    """Return (commits_behind, latest_commit_subject, error).

    `commits_behind` is None whenever the check could not run; `error` then
    holds a message worth showing the user. Failures are returned rather than
    raised so an offline machine never interrupts the app.
    """
    if not _is_git_checkout():
        return None, "", (
            "This copy wasn't installed with git, so it can't update itself.\n"
            f"Re-install with:  git clone {GITHUB_URL}.git"
        )

    _ensure_git_safe_directory()

    rc, out = _git(["fetch", "origin", UPDATE_BRANCH], timeout=timeout)
    if rc != 0:
        return None, "", out or "git fetch failed"

    rc, out = _git(["rev-list", f"HEAD..origin/{UPDATE_BRANCH}", "--count"], timeout=15)
    if rc != 0:
        return None, "", out or "git rev-list failed"
    try:
        behind = int(out)
    except ValueError:
        return None, "", f"Unexpected git output: {out!r}"

    subject = ""
    if behind:
        rc, out = _git(["log", "-1", "--pretty=%s",
                        f"origin/{UPDATE_BRANCH}"], timeout=15)
        if rc == 0:
            subject = out
    return behind, subject, None


def _restart_app() -> None:
    """Re-launch the app in a fresh, detached process and quit this one.

    os.execv is unreliable on Windows (the old window closes but no new one
    appears), so a detached child is spawned first and this process then exits.
    """
    args = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
    try:
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            # A detached process has no console, so give it null std handles —
            # otherwise the relaunched app dies on its first write to stdout.
            subprocess.Popen(
                args, cwd=_SCRIPT_DIR,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True)
        else:
            subprocess.Popen(args, cwd=_SCRIPT_DIR, start_new_session=True)
    except Exception as exc:
        messagebox.showerror(
            "Restart Failed",
            f"The update was installed but the app couldn't restart itself:\n\n{exc}\n\n"
            "Close this window and start the app again.")
        return
    os._exit(0)


# ════════════════════════════════════════════════════════════════════════════
# Updates Tab
# ════════════════════════════════════════════════════════════════════════════

class UpdatesTab:
    """Checks GitHub for a newer version and installs it in place.

    The app is distributed as a git clone, so an update is `git pull` — that
    keeps user-side installs a single command and needs no packaging step.
    """

    def __init__(self, parent: tk.Frame, root: tk.Tk,
                 notebook: ttk.Notebook = None, frame: tk.Frame = None) -> None:
        self.parent = parent
        self.root = root
        self._notebook = notebook      # used to badge the tab title when behind
        self._frame = frame
        self._busy = False
        self._build_ui()

    # ── UI ──────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        wrap = tk.Frame(self.parent, bg=BG)
        wrap.pack(fill=tk.BOTH, expand=True, padx=40, pady=30)

        tk.Label(wrap, text="Updates", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 22, "bold")).pack(anchor="w")
        tk.Label(wrap, text="Check GitHub for a newer version of Kash Cropper "
                            "and install it without leaving the app.",
                 bg=BG, fg=FG, font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 14))

        _separator(wrap)

        # Installed version / revision
        info = tk.Frame(wrap, bg=BG)
        info.pack(fill=tk.X, pady=(12, 0))
        rev = _local_revision()
        installed = f"Installed:  version {VERSION}"
        if rev:
            installed += f"  •  revision {rev}"
        tk.Label(info, text=installed, bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 10)).pack(anchor="w")
        tk.Label(info, text=f"Source:  {GITHUB_URL}", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 0))

        # Status line — the headline answer to "am I up to date?"
        self.status_lbl = tk.Label(wrap, text="Not checked yet.", bg=BG, fg=FG_DIM,
                                   font=("Segoe UI", 12, "bold"), justify=tk.LEFT,
                                   anchor="w", wraplength=900)
        self.status_lbl.pack(fill=tk.X, anchor="w", pady=(14, 2))

        self.detail_lbl = tk.Label(wrap, text="", bg=BG, fg=FG_DIM,
                                   font=("Segoe UI", 10), justify=tk.LEFT,
                                   anchor="w", wraplength=900)
        self.detail_lbl.pack(fill=tk.X, anchor="w", pady=(0, 12))

        btns = tk.Frame(wrap, bg=BG)
        btns.pack(fill=tk.X, pady=(0, 14))
        self.check_btn = tk.Button(btns, text="Check for Updates",
                                   command=self.check_for_updates,
                                   bg=BG_MID, fg=FG, activebackground=ACCENT,
                                   activeforeground=BG_DARK, relief=tk.FLAT,
                                   font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.check_btn.pack(side=tk.LEFT, ipadx=14, ipady=6)

        self.install_btn = tk.Button(btns, text="Download & Install Update",
                                     command=self.install_update,
                                     fg=BG_DARK, activebackground=GREEN,
                                     activeforeground=BG_DARK, relief=tk.FLAT,
                                     disabledforeground=FG_DIM,
                                     font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.install_btn.pack(side=tk.LEFT, padx=(12, 0), ipadx=14, ipady=6)
        self._enable_install(False)

        self.auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(btns, text="Check automatically at start-up",
                       variable=self.auto_var, bg=BG, fg=FG_DIM,
                       activebackground=BG, activeforeground=FG,
                       selectcolor=BG_DARK, font=("Segoe UI", 9),
                       highlightthickness=0, bd=0).pack(side=tk.LEFT, padx=(18, 0))

        _separator(wrap)

        tk.Label(wrap, text="Log", bg=BG, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(10, 4))

        log_wrap = tk.Frame(wrap, bg=BG)
        log_wrap.pack(fill=tk.BOTH, expand=True)
        sb = tk.Scrollbar(log_wrap)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log = tk.Text(log_wrap, bg=BG_DARK, fg=FG, insertbackground=FG,
                           font=("Consolas", 9), relief=tk.FLAT, wrap=tk.WORD,
                           yscrollcommand=sb.set, state=tk.DISABLED)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=self.log.yview)

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _log(self, text: str) -> None:
        """Append a line to the log pane. Main thread only."""
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, text.rstrip() + "\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _enable_install(self, enabled: bool) -> None:
        """Enable the install button. A disabled tk Button keeps its own
        background, so the colour has to be swapped alongside the state or a
        greyed-out button still reads as clickable."""
        self.install_btn.config(state=tk.NORMAL if enabled else tk.DISABLED,
                                bg=GREEN if enabled else BG_MID,
                                cursor="hand2" if enabled else "arrow")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.check_btn.config(state=tk.DISABLED if busy else tk.NORMAL)
        if busy:
            self._enable_install(False)

    def _badge_tab(self, behind) -> None:
        """Mark the tab title so a pending update is visible from any tab."""
        if self._notebook is None or self._frame is None:
            return
        try:
            label = "  Updates  ●   " if behind else "  Updates       "
            self._notebook.tab(self._frame, text=label)
        except tk.TclError:
            pass

    # ── Check ───────────────────────────────────────────────────────────────
    def check_for_updates(self, silent: bool = False) -> None:
        """Fetch remote metadata and report how far behind this copy is.

        `silent` is used for the automatic start-up check: it skips the log
        noise and leaves the status line unchanged if the check can't run.
        """
        if self._busy:
            return
        self._set_busy(True)
        self.status_lbl.config(text="Checking for updates…", fg=YELLOW)
        self.detail_lbl.config(text="")
        if not silent:
            self._log(f"Checking {GITHUB_URL} ({UPDATE_BRANCH})…")

        def _work():
            result = _check_for_update()
            self.root.after(0, self._apply_check_result, silent, *result)

        threading.Thread(target=_work, daemon=True).start()

    def _apply_check_result(self, silent, behind, subject, error) -> None:
        self._set_busy(False)

        if error is not None:
            self._badge_tab(0)
            if silent:
                # Offline at start-up is not worth alarming anyone about.
                self.status_lbl.config(text="Update status unknown.", fg=FG_DIM)
                self.detail_lbl.config(text="Click “Check for Updates” to try again.")
                return
            self.status_lbl.config(text="Could not check for updates", fg=YELLOW)
            self.detail_lbl.config(text=error)
            self._log(error)
            return

        if behind == 0:
            self.status_lbl.config(text="✓  You are up to date", fg=GREEN)
            self.detail_lbl.config(
                text=f"No new commits on origin/{UPDATE_BRANCH}.")
            self._enable_install(False)
            self._badge_tab(0)
            if not silent:
                self._log("Up to date.")
            return

        plural = "commit" if behind == 1 else "commits"
        self.status_lbl.config(
            text=f"⬇  Update available — {behind} new {plural}", fg=RED)
        self.detail_lbl.config(
            text=(f"Latest: {subject}" if subject else
                  "Click “Download & Install Update” to apply it."))
        self._enable_install(True)
        self._badge_tab(behind)
        self._log(f"Update available: {behind} new {plural} on origin/{UPDATE_BRANCH}.")

    # ── Install ─────────────────────────────────────────────────────────────
    def install_update(self) -> None:
        if self._busy:
            return
        if not messagebox.askyesno(
            "Install Update",
            "This downloads the latest version from GitHub and restarts "
            "Kash Cropper.\n\nAny running job will be lost. Continue?"
        ):
            return

        self._set_busy(True)
        self.status_lbl.config(text="Installing update…", fg=YELLOW)
        self.detail_lbl.config(text="")
        self._log("")
        self._log("Pulling the latest version from GitHub…")

        def _work():
            _ensure_git_safe_directory()
            rc, out = _git(["pull", "origin", UPDATE_BRANCH], timeout=180)
            stashed = False

            # A pull usually fails because local edits or untracked files in the
            # app folder would be overwritten. Set those aside in a RECOVERABLE
            # stash and retry once, so nobody needs the command line.
            if rc != 0:
                self.root.after(0, self._log, out)
                self.root.after(0, self._log,
                                "Pull blocked — setting local changes aside in a "
                                "recoverable git stash and retrying…")
                srt, _ = _git(["stash", "push", "--include-untracked",
                               "-m", "Kash Cropper auto-update stash"], timeout=60)
                stashed = (srt == 0)
                rc, out = _git(["pull", "origin", UPDATE_BRANCH], timeout=180)

            self.root.after(0, self._apply_install_result, rc, out, stashed)

        threading.Thread(target=_work, daemon=True).start()

    def _apply_install_result(self, rc, out, stashed) -> None:
        self._log(out)

        if rc != 0:
            self._set_busy(False)
            self.status_lbl.config(text="Update failed", fg=RED)
            self.detail_lbl.config(text="See the log below for what git reported.")
            messagebox.showerror(
                "Update Failed",
                "The update could not be applied:\n\n"
                f"{out[-600:]}\n\n"
                "The installed version is unchanged.")
            return

        self.status_lbl.config(text="✓  Update installed — restarting…", fg=GREEN)
        self._badge_tab(0)
        self._log("Update installed. Restarting in 2 seconds…")

        if stashed:
            messagebox.showinfo(
                "Update Installed",
                "The update was installed.\n\n"
                "Some local changes in the app folder were set aside in a git "
                "stash so the update could apply — they are NOT lost. Run "
                "'git stash pop' in the app folder to restore them.\n\n"
                "Kash Cropper will now restart.")

        self.root.after(2000, _restart_app)


# ════════════════════════════════════════════════════════════════════════════
# Splash screen
# ════════════════════════════════════════════════════════════════════════════

class SplashScreen:
    """Borderless credit card shown while the main window is being built.

    The card sizes itself from its own content rather than a fixed pixel box,
    so it stays intact under Windows display scaling.
    """

    MIN_MS = 1800          # keep the credit on screen at least this long

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._t0 = time.monotonic()

        win = tk.Toplevel(root)
        self.win = win
        win.overrideredirect(True)       # no title bar / borders
        win.configure(bg=ACCENT)         # shows through as a 1 px accent frame
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass

        card = tk.Frame(win, bg=BG_DARK)
        card.pack(padx=1, pady=1)

        body = tk.Frame(card, bg=BG_DARK)
        body.pack(padx=54, pady=(38, 30))

        tk.Label(body, text=APP_NAME, bg=BG_DARK, fg=ACCENT,
                 font=("Segoe UI", 34, "bold")).pack()
        tk.Label(body, text="Video Cropping & Clipping Tool", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 13)).pack(pady=(2, 0))

        tk.Frame(body, bg=ACCENT, height=2).pack(fill=tk.X, pady=22)

        tk.Label(body, text="Created by Jobe Ritchie, PhD", bg=BG_DARK, fg=FG,
                 font=("Segoe UI", 14, "bold")).pack()
        tk.Label(body, text="University of North Carolina at Chapel Hill",
                 bg=BG_DARK, fg=PURPLE, font=("Segoe UI", 12)).pack(pady=(4, 0))

        tk.Label(body, text=f"Version {VERSION}", bg=BG_DARK, fg=FG_DIM,
                 font=("Segoe UI", 10)).pack(pady=(22, 0))
        tk.Label(body, text="Starting…", bg=BG_DARK, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(pady=(4, 0))

        self._centre()
        win.update()                     # paint before the main UI build blocks

    def _centre(self) -> None:
        """Place the card at its natural size, centred on the screen."""
        win = self.win
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = max(0, (win.winfo_screenwidth() - w) // 2)
        y = max(0, (win.winfo_screenheight() - h) // 3)
        win.geometry(f"{w}x{h}+{x}+{y}")

    def finish(self, on_done=None) -> None:
        """Close the splash once MIN_MS has elapsed, then run `on_done`."""
        elapsed_ms = int((time.monotonic() - self._t0) * 1000)
        self.root.after(max(0, self.MIN_MS - elapsed_ms), self._close, on_done)

    def _close(self, on_done) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if on_done is not None:
            on_done()


# ════════════════════════════════════════════════════════════════════════════
# Welcome Tab  (landing page)
# ════════════════════════════════════════════════════════════════════════════

class WelcomeTab:
    """Landing page: app name, version, a short guide to each tab, and the
    detected processing back-end (ffmpeg / GPU)."""

    _TABS = [
        ("Crop Regions", "Draw multiple rectangles on one video and export each "
                          "region as its own file."),
        ("Batch Crop",   "Draw one crop per video across a batch; outputs keep "
                          "the original name in a 'cropped' folder."),
        ("Clip / Trim",  "Scrub frame-by-frame, set in/out points, and export "
                          "trimmed clips."),
        ("Brightness",   "Measure and equalise luminance across many videos."),
        ("Stretch",      "Force every video to one exact resolution."),
        ("Convert",      "Batch re-encode legacy formats (e.g. MPG) to MP4."),
        ("Split",        "Halve each video down the middle; output names are "
                          "parsed from the source filename and editable."),
        ("Updates",      "Check GitHub for a newer version and install it "
                          "in place."),
    ]

    def __init__(self, parent: tk.Frame, root: tk.Tk) -> None:
        self.parent = parent
        self.root = root
        self._build_ui()

    def _build_ui(self) -> None:
        wrap = tk.Frame(self.parent, bg=BG)
        wrap.pack(fill=tk.BOTH, expand=True, padx=40, pady=30)

        tk.Label(wrap, text=APP_NAME, bg=BG, fg=ACCENT,
                 font=("Segoe UI", 30, "bold")).pack(anchor="w")
        tk.Label(wrap, text=f"Version {VERSION}", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 12)).pack(anchor="w", pady=(0, 4))
        tk.Label(wrap, text="A toolkit for cropping, clipping, splitting and "
                            "batch-processing videos.",
                 bg=BG, fg=FG, font=("Segoe UI", 11)).pack(anchor="w", pady=(0, 16))

        _separator(wrap)

        tk.Label(wrap, text="Tabs", bg=BG, fg=FG,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(12, 6))

        grid = tk.Frame(wrap, bg=BG)
        grid.pack(fill=tk.X, anchor="w")
        for i, (name, desc) in enumerate(self._TABS):
            row = tk.Frame(grid, bg=BG)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=name, bg=BG, fg=PURPLE, width=14, anchor="w",
                     font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
            tk.Label(row, text=desc, bg=BG, fg=FG, anchor="w", justify=tk.LEFT,
                     wraplength=820, font=("Segoe UI", 10)).pack(side=tk.LEFT, fill=tk.X)

        _separator(wrap)

        # Detected processing back-end
        if _ffmpeg_available():
            engine = "ffmpeg detected"
            engine += "  •  NVENC GPU encoding available" if _nvenc_available() \
                else "  •  CPU encoding (NVENC not available)"
            color = GREEN
        else:
            engine = "ffmpeg not found — using the limited OpenCV fallback"
            color = YELLOW
        tk.Label(wrap, text=engine, bg=BG, fg=color,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 0))
        tk.Label(wrap, text="Select a tab above to get started.",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 10)).pack(anchor="w", pady=(8, 0))

        tk.Label(wrap, text="Created by Jobe Ritchie, PhD  \u2022  University of "
                            "North Carolina at Chapel Hill",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=(18, 0))


# ════════════════════════════════════════════════════════════════════════════
# App shell
# ════════════════════════════════════════════════════════════════════════════

class KashCropperApp:
    def __init__(self, root: tk.Tk) -> None:
        root.title(f"{APP_NAME}  v{VERSION}")
        root.geometry("1340x780")
        root.configure(bg=BG)
        root.minsize(960, 600)

        style = ttk.Style()
        style.theme_use("clam")
        for name, color in (("Crop", GREEN), ("Clip", ACCENT), ("BatchCrop", GREEN),
                            ("Brightness", PURPLE), ("Stretch", YELLOW),
                            ("Convert", ACCENT), ("Split", RED)):
            style.configure(f"{name}.Horizontal.TProgressbar",
                            troughcolor=BG_MID, background=color,
                            bordercolor=BG_MID, lightcolor=color, darkcolor=color)
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=0)
        style.configure("TNotebook.Tab", background=BG_MID, foreground=FG_DIM,
                        font=("Segoe UI", 11, "bold"), padding=(22, 9))
        style.map("TNotebook.Tab",
                  background=[("selected", BG_DARK)],
                  foreground=[("selected", ACCENT)])
        style.configure("TCombobox", fieldbackground=BG_MID, background=BG_MID,
                        foreground=FG, selectbackground=ACCENT, selectforeground=BG_DARK,
                        arrowcolor=FG)
        style.map("TCombobox", fieldbackground=[("readonly", BG_MID)])

        # Shared style for the file-queue Treeviews (file name + folder column)
        style.configure("Queue.Treeview", background=BG_MID, foreground=FG,
                        fieldbackground=BG_MID, font=("Segoe UI", 9),
                        rowheight=22, borderwidth=0)
        style.configure("Queue.Treeview.Heading", background=BG_DARK,
                        foreground=ACCENT, font=("Segoe UI", 9, "bold"), relief=tk.FLAT)
        style.map("Queue.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", BG_DARK)])

        nb = ttk.Notebook(root)
        nb.pack(fill=tk.BOTH, expand=True)

        welcome_frame    = tk.Frame(nb, bg=BG)
        crop_frame       = tk.Frame(nb, bg=BG)
        clip_frame       = tk.Frame(nb, bg=BG)
        batch_frame      = tk.Frame(nb, bg=BG)
        brightness_frame = tk.Frame(nb, bg=BG)
        stretch_frame    = tk.Frame(nb, bg=BG)
        convert_frame    = tk.Frame(nb, bg=BG)
        split_frame      = tk.Frame(nb, bg=BG)
        updates_frame    = tk.Frame(nb, bg=BG)
        nb.add(welcome_frame,    text="  Welcome       ")
        nb.add(crop_frame,       text="  Crop Regions  ")
        nb.add(batch_frame,      text="  Batch Crop    ")
        nb.add(clip_frame,       text="  Clip / Trim   ")
        nb.add(brightness_frame, text="  Brightness    ")
        nb.add(stretch_frame,    text="  Stretch       ")
        nb.add(convert_frame,    text="  Convert       ")
        nb.add(split_frame,      text="  Split         ")
        nb.add(updates_frame,    text="  Updates       ")

        WelcomeTab(welcome_frame, root)
        CropTab(crop_frame, root)
        BatchCropTab(batch_frame, root)

        # The Clip tab hosts two sub-tabs: the original Trim/Keep trimmer and
        # the new Cut & Stitch editor (mark ranges to remove, splice the rest).
        clip_nb = ttk.Notebook(clip_frame)
        clip_nb.pack(fill=tk.BOTH, expand=True)
        clip_trim_frame = tk.Frame(clip_nb, bg=BG)
        clip_cut_frame = tk.Frame(clip_nb, bg=BG)
        clip_nb.add(clip_trim_frame, text="  Trim / Keep   ")
        clip_nb.add(clip_cut_frame, text="  Cut & Stitch  ")
        ClipTab(clip_trim_frame, root)
        CutStitchTab(clip_cut_frame, root)
        BrightnessTab(brightness_frame, root)
        StretchTab(stretch_frame, root)
        ConvertTab(convert_frame, root)
        SplitTab(split_frame, root)
        self.updates = UpdatesTab(updates_frame, root, nb, updates_frame)

        # Look for a new version in the background once the window is up, so a
        # pending update badges the Updates tab without the user asking.
        root.after(1500, self._auto_check_updates)

        # Pre-decode the completion chime to WAV in the background so the first
        # "job done" sound isn't delayed by the one-time MP3→WAV conversion.
        threading.Thread(target=_ensure_cash_wav, daemon=True).start()

    def _auto_check_updates(self) -> None:
        """Start-up update check. Quiet on failure — being offline is normal."""
        if self.updates.auto_var.get():
            self.updates.check_for_updates(silent=True)


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    root = tk.Tk()
    root.withdraw()                      # stay hidden behind the splash

    splash = SplashScreen(root)
    KashCropperApp(root)                 # built while the splash is up
    splash.finish(on_done=root.deiconify)

    root.mainloop()


if __name__ == "__main__":
    main()
