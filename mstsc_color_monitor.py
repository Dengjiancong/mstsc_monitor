"""Monitor a visible screen region and send ntfy alerts when its colors change."""

from __future__ import annotations

import ctypes
import json
import re
import secrets
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "monitor_config.json"
TOPIC_RE = re.compile(r"[-_A-Za-z0-9]{1,64}\Z")
SAMPLES_PER_AXIS = 12
CONFIRM_SAMPLES = 3
COOLDOWN_SECONDS = 30

if not hasattr(ctypes, "windll"):
    raise SystemExit("这个程序只能在 Windows 桌面运行。")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:
    user32.SetProcessDPIAware()
except OSError:
    pass
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.GetDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = ctypes.c_uint32


def default_config() -> dict:
    return {
        "region": None,
        "topic": "mstsc-" + secrets.token_hex(12),
        "interval": 1.0,
        "color_threshold": 30,
        "changed_percent": 10,
    }


def load_config() -> dict:
    config = default_config()
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update(saved)
        except (OSError, ValueError):
            pass
    return config


def sample_region(region: tuple[int, int, int, int]) -> tuple[tuple[int, int, int], ...]:
    x1, y1, x2, y2 = region
    dc = user32.GetDC(None)
    if not dc:
        raise RuntimeError("无法读取屏幕画面")
    pixels = []
    try:
        for row in range(SAMPLES_PER_AXIS):
            y = y1 + (y2 - y1) * (2 * row + 1) // (2 * SAMPLES_PER_AXIS)
            for col in range(SAMPLES_PER_AXIS):
                x = x1 + (x2 - x1) * (2 * col + 1) // (2 * SAMPLES_PER_AXIS)
                color = gdi32.GetPixel(dc, x, y)
                if color == 0xFFFFFFFF:
                    raise RuntimeError("所选区域超出当前屏幕，或屏幕画面不可读取")
                pixels.append((color & 255, (color >> 8) & 255, (color >> 16) & 255))
    finally:
        user32.ReleaseDC(None, dc)
    return tuple(pixels)


def changed_fraction(previous, current, color_threshold: int) -> float:
    changed = sum(
        max(abs(a - b) for a, b in zip(old, new)) >= color_threshold
        for old, new in zip(previous, current)
    )
    return changed / len(previous)


def send_ntfy(topic: str, message: str) -> None:
    request = Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": "MSTSC color monitor", "Priority": "high"},
        method="POST",
    )
    with urlopen(request, timeout=12) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"提醒服务返回 HTTP {response.status}")


class RegionPicker(tk.Toplevel):
    def __init__(self, parent: tk.Tk, on_pick, on_cancel):
        super().__init__(parent)
        self.on_pick = on_pick
        self.on_cancel = on_cancel
        self.start = None
        left = user32.GetSystemMetrics(76)
        top = user32.GetSystemMetrics(77)
        width = user32.GetSystemMetrics(78)
        height = user32.GetSystemMetrics(79)
        self.geometry(f"{width}x{height}{left:+d}{top:+d}")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.35)
        self.configure(bg="#1b283a")
        self.canvas = tk.Canvas(self, bg="#1b283a", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            width // 2, 35, text="拖动框选 mstsc 中要监控的区域；按 Esc 取消",
            fill="white", font=("Microsoft YaHei UI", 16), tags="help"
        )
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.bind("<Escape>", lambda _event: self.cancel())
        self.focus_force()
        self.grab_set()

    def press(self, event):
        self.start = (event.x_root, event.y_root)

    def drag(self, event):
        if self.start is None:
            return
        x0, y0 = self.start
        self.canvas.delete("selection")
        self.canvas.create_rectangle(
            x0 - self.winfo_rootx(), y0 - self.winfo_rooty(),
            event.x_root - self.winfo_rootx(), event.y_root - self.winfo_rooty(),
            outline="#7ee6ff", width=3, fill="#4ea8bf", tags="selection"
        )

    def release(self, event):
        if self.start is None:
            return
        x0, y0 = self.start
        region = (min(x0, event.x_root), min(y0, event.y_root),
                  max(x0, event.x_root), max(y0, event.y_root))
        self.destroy()
        if region[2] - region[0] >= 10 and region[3] - region[1] >= 10:
            self.on_pick(region)
        else:
            self.on_cancel()
            messagebox.showinfo("区域太小", "请拖动选择至少 10×10 像素的区域。")

    def cancel(self):
        self.destroy()
        self.on_cancel()


class MonitorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = load_config()
        self.region = self._valid_region(self.config.get("region"))
        self.running = False
        self.baseline = None
        self.pending = 0
        self.last_alert = 0.0
        self.topic = tk.StringVar(value=str(self.config.get("topic", "")))
        self.interval = tk.StringVar(value=str(self.config.get("interval", 1.0)))
        self.threshold = tk.StringVar(value=str(self.config.get("color_threshold", 30)))
        self.changed_percent = tk.StringVar(value=str(self.config.get("changed_percent", 10)))
        self.status = tk.StringVar(value="待启动")
        self.region_text = tk.StringVar()
        self._update_region_text()
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self.close)

    @staticmethod
    def _valid_region(value):
        if isinstance(value, (list, tuple)) and len(value) == 4 and all(type(x) is int for x in value):
            if value[2] > value[0] and value[3] > value[1]:
                return tuple(value)
        return None

    def _build_ui(self):
        self.root.title("MSTSC 区域颜色监控")
        self.root.geometry("590x480")
        self.root.minsize(550, 450)
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="监控屏幕区域的颜色变化", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="请保持 mstsc 窗口可见；选取颜色会变化的部分。", foreground="#555555").pack(anchor="w", pady=(4, 16))
        area = ttk.LabelFrame(frame, text="监控区域", padding=10)
        area.pack(fill="x")
        ttk.Label(area, textvariable=self.region_text).pack(side="left", fill="x", expand=True)
        ttk.Button(area, text="框选区域", command=self.pick_region).pack(side="right")

        notify = ttk.LabelFrame(frame, text="手机提醒（ntfy）", padding=10)
        notify.pack(fill="x", pady=12)
        ttk.Label(notify, text="手机 ntfy 应用订阅这个主题：").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Entry(notify, textvariable=self.topic, width=48).grid(row=1, column=0, sticky="ew", pady=7)
        ttk.Button(notify, text="复制", command=self.copy_topic).grid(row=1, column=1, padx=6)
        ttk.Button(notify, text="测试提醒", command=self.test_notification).grid(row=1, column=2)
        notify.columnconfigure(0, weight=1)
        ttk.Label(notify, text="主题相当于接收地址，请勿使用容易猜到的名称。", foreground="#666666").grid(row=2, column=0, columnspan=3, sticky="w")

        settings = ttk.LabelFrame(frame, text="检测设置", padding=10)
        settings.pack(fill="x")
        for col, (label, var, suffix) in enumerate((
            ("检查间隔", self.interval, "秒"),
            ("颜色差阈值", self.threshold, "RGB"),
            ("变化采样点", self.changed_percent, "%"),
        )):
            ttk.Label(settings, text=label).grid(row=0, column=col, sticky="w", padx=(0, 12))
            ttk.Entry(settings, textvariable=var, width=8).grid(row=1, column=col, sticky="w", pady=5)
            ttk.Label(settings, text=suffix).grid(row=1, column=col, sticky="e", padx=(80, 18))

        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=16)
        self.start_button = ttk.Button(controls, text="开始监控", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Label(controls, textvariable=self.status).pack(side="right")
        self.log_box = tk.Text(frame, height=6, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True)
        self.log("程序已就绪。先在手机订阅主题，再测试提醒。")

    def log(self, message: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _update_region_text(self):
        if self.region is None:
            self.region_text.set("尚未选择")
        else:
            x1, y1, x2, y2 = self.region
            self.region_text.set(f"({x1}, {y1}) · {x2-x1}×{y2-y1} 像素")

    def pick_region(self):
        self.stop()
        self.root.withdraw()
        self.root.after(250, lambda: RegionPicker(self.root, self.region_selected, self.restore_window))

    def restore_window(self):
        self.root.deiconify()
        self.root.lift()

    def region_selected(self, region):
        self.region = region
        self._update_region_text()
        self.save_config()
        self.restore_window()
        self.log("已更新监控区域。")

    def copy_topic(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.topic.get().strip())
        self.log("已复制主题名称。")

    def validate_settings(self):
        topic = self.topic.get().strip()
        if not TOPIC_RE.fullmatch(topic):
            raise ValueError("主题只能包含英文字母、数字、横线和下划线，最多 64 个字符。")
        interval = float(self.interval.get())
        threshold = int(self.threshold.get())
        percent = int(self.changed_percent.get())
        if not 0.2 <= interval <= 60:
            raise ValueError("检查间隔应在 0.2 至 60 秒之间。")
        if not 1 <= threshold <= 255:
            raise ValueError("颜色差阈值应在 1 至 255 之间。")
        if not 1 <= percent <= 100:
            raise ValueError("变化采样点应在 1% 至 100% 之间。")
        return topic, interval, threshold, percent

    def save_config(self):
        try:
            topic, interval, threshold, percent = self.validate_settings()
            config = {"region": self.region, "topic": topic, "interval": interval,
                      "color_threshold": threshold, "changed_percent": percent}
            CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except (ValueError, OSError):
            pass

    def start(self):
        if self.region is None:
            messagebox.showwarning("先选区域", "请先框选需要监控的区域。")
            return
        try:
            self.active_topic, self.active_interval, self.active_threshold, self.active_percent = self.validate_settings()
        except ValueError as error:
            messagebox.showerror("设置有误", str(error))
            return
        self.save_config()
        self.running = True
        self.baseline = None
        self.pending = 0
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set("监控中")
        self.log("开始监控；连续 3 次观察到变化后会发送提醒。")
        self.root.after(400, self.poll)

    def stop(self):
        if self.running:
            self.running = False
            self.status.set("已停止")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.log("已停止监控。")

    def poll(self):
        if not self.running:
            return
        try:
            current = sample_region(self.region)
        except RuntimeError as error:
            self.stop()
            self.log(f"读取屏幕失败：{error}")
            return
        if self.baseline is None:
            self.baseline = current
            self.log("已记录初始画面颜色。")
        else:
            fraction = changed_fraction(self.baseline, current, self.active_threshold)
            if fraction * 100 >= self.active_percent:
                self.pending += 1
                if self.pending >= CONFIRM_SAMPLES:
                    self.baseline = current
                    self.pending = 0
                    if time.monotonic() - self.last_alert >= COOLDOWN_SECONDS:
                        self.last_alert = time.monotonic()
                        percent = round(fraction * 100)
                        self.log(f"检测到颜色变化（约 {percent}% 采样点），正在发送提醒。")
                        self.notify_async(
                            f"检测到 MSTSC 监控区域颜色变化（约 {percent}% 采样点）。时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                            self.active_topic,
                        )
                    else:
                        self.log("检测到新变化，处于 30 秒提醒冷却期。")
            else:
                self.pending = 0
        self.root.after(round(self.active_interval * 1000), self.poll)

    def notify_async(self, message, topic):

        def worker():
            try:
                send_ntfy(topic, message)
            except (HTTPError, URLError, OSError, RuntimeError) as error:
                result = f"提醒发送失败：{error}"
            else:
                result = "提醒已发送到 ntfy。"
            try:
                self.root.after(0, self.log, result)
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def test_notification(self):
        try:
            topic, _, _, _ = self.validate_settings()
        except ValueError as error:
            messagebox.showerror("设置有误", str(error))
            return
        self.save_config()
        self.log("正在发送测试提醒。")
        self.notify_async(f"MSTSC 颜色监控测试提醒。时间：{datetime.now():%Y-%m-%d %H:%M:%S}", topic)

    def close(self):
        self.stop()
        self.save_config()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()
