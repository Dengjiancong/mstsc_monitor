"""GUI for covered-window MSTSC completion capture and Feishu notification."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from completion_core import CompletionLatch, GREEN, PURPLE, UNKNOWN, UnknownFrameWatch, classify_status
from completion_messages import message_for
from feishu_command_listener import FeishuCommandListener
from feishu_commands import SCREENSHOT_REPLY
from feishu_client import (get_tenant_token, list_bot_chats, send_completion,
                           send_gif, send_image_key, send_text, send_text_token,
                           upload_image)
from gif_picker import choose_gif
from monitor_health import MAX_CAPTURE_FAILURES, MonitorHealth, watchdog_timeout
from notification_outbox import (NotificationOutbox, capture_time_from_filename,
                                 capture_time_text)
from win_capture import CapturedWindow, capture_window, list_mstsc_windows, user32
from windows_secret import protect, unprotect


APP_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
           else Path(__file__).resolve().parent)
CONFIG_FILE = APP_DIR / "completion_config.json"
SCREENSHOT_DIR = APP_DIR / "screenshots"
GIF_DIR = APP_DIR / "gif"
PENDING_DIR = APP_DIR / "pending_notifications"
RECIPIENT_TYPES = {"群聊 Chat ID": "chat_id", "私聊：邮箱": "email",
                   "私聊：Open ID": "open_id", "私聊：User ID": "user_id"}
UNKNOWN_ALERT_SECONDS = 30.0
MAX_PENDING_CAPTURES = 3


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_screenshot(capture: CapturedWindow, captured_at: datetime | None = None) -> Path:
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    name = (captured_at or datetime.now()).strftime("completion_%Y%m%d_%H%M%S_%f.png")
    path = SCREENSHOT_DIR / name
    path.write_bytes(capture.png())
    return path


def stored_screenshots(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("completion_*.png")
                  if path.is_file() and not path.is_symlink())


def remove_screenshots(paths: list[Path]) -> tuple[int, list[str]]:
    removed = 0
    failures = []
    for path in paths:
        try:
            path.unlink()
            removed += 1
        except OSError as error:
            failures.append(f"{path.name}: {error}")
    return removed, failures


class RegionPicker(tk.Toplevel):
    def __init__(self, parent, on_pick, on_cancel):
        super().__init__(parent)
        self.on_pick, self.on_cancel = on_pick, on_cancel
        self.start = None
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        self.geometry(f"{width}x{height}{left:+d}{top:+d}")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.25)
        self.canvas = tk.Canvas(self, bg="#213248", cursor="crosshair", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(width // 2, 35,
                                text="拖动框选右上角 Testing / credence 状态框；按 Esc 取消",
                                fill="white", font=("Microsoft YaHei UI", 16))
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.bind("<Escape>", lambda _event: self.cancel())
        self.focus_force()
        self.grab_set()

    def press(self, event):
        self.start = (event.x_root, event.y_root)

    def drag(self, event):
        if not self.start:
            return
        x0, y0 = self.start
        self.canvas.delete("box")
        self.canvas.create_rectangle(x0 - self.winfo_rootx(), y0 - self.winfo_rooty(),
                                     event.x_root - self.winfo_rootx(), event.y_root - self.winfo_rooty(),
                                     outline="#70e8ff", width=3, tags="box")

    def release(self, event):
        if not self.start:
            return
        x0, y0 = self.start
        region = (min(x0, event.x_root), min(y0, event.y_root),
                  max(x0, event.x_root), max(y0, event.y_root))
        self.destroy()
        if region[2] - region[0] >= 20 and region[3] - region[1] >= 15:
            self.on_pick(region)
        else:
            self.on_cancel()
            messagebox.showwarning("区域太小", "请框选完整的右上角状态框。")

    def cancel(self):
        self.destroy()
        self.on_cancel()


class MonitorTask:
    """Independent UI and runtime state for one of the four MSTSC monitors."""

    def __init__(self, root, key, saved):
        self.key = key
        self.name = f"{key}设备任务"
        self.enabled = tk.BooleanVar(root, value=bool(saved.get("enabled", key == "A")))
        self.window_name = tk.StringVar(root)
        self.region_name = tk.StringVar(root)
        self.interval = tk.StringVar(root, value=str(saved.get("interval", 1.0)))
        self.state = tk.StringVar(root, value="未启动")
        self.region = App._region(saved.get("region"))
        self.window_title = str(saved.get("window_title", ""))
        self.selected_hwnd = None
        self.stop_event = threading.Event()
        self.worker = None
        self.health = None
        self.credentials = None
        self.monitor_interval = 1.0
        self.window_box = self.start_btn = self.stop_btn = None
        self.monitor_light = self.monitor_light_dot = None
        self.light_state = "red"


class App:
    def __init__(self, root):
        self.root = root
        self.config = load_config()
        self.events = queue.Queue()
        self.deliveries = queue.Queue()
        self.outbox = NotificationOutbox(PENDING_DIR, SCREENSHOT_DIR)
        self.shutdown_event = threading.Event()
        self.delivery_attempts = {}
        self.command_listener = None
        self.background_screenshots = 0
        self.windows = {}
        self.window_titles = {}
        self.chats = {}
        saved_tasks = self.config.get("tasks") if isinstance(self.config.get("tasks"), dict) else {}
        if not saved_tasks:
            saved_tasks = {"A": {"enabled": True, "region": self.config.get("region"),
                                 "interval": self.config.get("interval", 1.0)}}
        self.tasks = {key: MonitorTask(root, key, saved_tasks.get(key, {}))
                      for key in ("A", "B", "C", "D")}
        self.app_id = tk.StringVar(value=self.config.get("app_id", ""))
        self.chat_id = tk.StringVar(value=self.config.get("chat_id", ""))
        default_authorized = (self.config.get("chat_id", "")
                              if self.config.get("recipient_type") in ("user_id", "open_id") else "")
        self.authorized_id = tk.StringVar(value=self.config.get("authorized_id", default_authorized))
        self.command_enabled = tk.BooleanVar(value=self.config.get("command_enabled", False))
        self.command_state = tk.StringVar(value="截图指令未开启")
        self.recipient_choice = tk.StringVar(value=next(
            (name for name, api_type in RECIPIENT_TYPES.items()
             if api_type == self.config.get("recipient_type", "chat_id")), "群聊 Chat ID"))
        # Existing installations get the new off-by-default setting once;
        # subsequent explicit choices are saved normally.
        gif_default_applied = self.config.get("gif_default_off_applied", False)
        self.gif_enabled = tk.BooleanVar(value=self.config.get("gif_enabled", False)
                                        if gif_default_applied else False)
        self.gif_enabled_value = self.gif_enabled.get()
        secret = ""
        if self.config.get("secret_dpapi"):
            try:
                secret = unprotect(self.config["secret_dpapi"])
            except (OSError, ValueError):
                pass
        self.app_secret = tk.StringVar(value=secret)
        self._build_ui()
        pending_ids = self.outbox.pending_ids()
        for task_id in pending_ids:
            self.deliveries.put((task_id, None))
        if pending_ids:
            self.log(f"已找到 {len(pending_ids)} 条未完成的飞书提醒，正在继续补发。")
        self.refresh_windows()
        for task in self.tasks.values():
            self._update_region(task)
        self.root.after(100, self.process_events)
        self.root.after(1000, self.check_monitor_health)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self.delivery_loop, daemon=True).start()
        if self.command_enabled.get():
            self.root.after(500, self.start_commands)

    @staticmethod
    def _region(value):
        if isinstance(value, (list, tuple)) and len(value) == 4 and all(type(x) is int for x in value):
            if value[2] > value[0] and value[3] > value[1]:
                return tuple(value)
        return None

    def _build_ui(self):
        self.root.title("MSTSC 测试结束监控")
        self.root.geometry("720x900")
        self.root.minsize(680, 760)
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Testing → credence 测试结束监控",
                  font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="检测右上角状态框；测试结束时截图并发到飞书。",
                  foreground="#555555").pack(anchor="w", pady=(4, 12))

        self.task_tabs = ttk.Notebook(frame)
        self.task_tabs.pack(fill="x")
        for task in self.tasks.values():
            tab = ttk.Frame(self.task_tabs, padding=10)
            self.task_tabs.add(tab, text=task.name)
            self._build_task_tab(tab, task)

        feishu = ttk.LabelFrame(frame, text="飞书应用机器人", padding=10)
        feishu.pack(fill="x", pady=12)
        for rowno, (label, variable, hide) in enumerate((
            ("App ID", self.app_id, ""), ("App Secret", self.app_secret, "*"),
        )):
            ttk.Label(feishu, text=label, width=13).grid(row=rowno, column=0, sticky="w", pady=4)
            ttk.Entry(feishu, textvariable=variable, show=hide).grid(row=rowno, column=1, sticky="ew", pady=4)
        ttk.Label(feishu, text="接收方式", width=13).grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(feishu, textvariable=self.recipient_choice,
                     values=list(RECIPIENT_TYPES), state="readonly", width=18).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(feishu, text="接收地址", width=13).grid(row=3, column=0, sticky="w", pady=4)
        self.chat_box = ttk.Combobox(feishu, textvariable=self.chat_id)
        self.chat_box.grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Button(feishu, text="获取群聊", command=self.fetch_chats).grid(row=3, column=2, padx=(8, 0))
        self.chat_box.bind("<<ComboboxSelected>>", self.chat_selected)
        feishu.columnconfigure(1, weight=1)
        ttk.Label(feishu, text="私聊可填你的飞书邮箱；App Secret 会尝试加密保存。",
                  foreground="#666666").grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Checkbutton(frame, text="发送随机 GIF 表情包", variable=self.gif_enabled,
                        command=self.on_gif_toggle).pack(anchor="w", pady=(8, 0))

        commands = ttk.LabelFrame(frame, text="飞书私聊截图指令", padding=10)
        commands.pack(fill="x", pady=(10, 0))
        ttk.Label(commands, text="允许截图者 User ID / Open ID").pack(side="left")
        ttk.Entry(commands, textvariable=self.authorized_id, width=19).pack(side="left", padx=8)
        self.command_btn = ttk.Button(commands, text="开启指令", command=self.toggle_commands)
        self.command_btn.pack(side="left")
        ttk.Label(commands, textvariable=self.command_state).pack(side="right")
        ttk.Label(frame, text="私聊发送“截图A”或“jta”截取 A 任务；B/C/D 同理，英文字母不区分大小写。",
                  foreground="#666666").pack(anchor="w", pady=(3, 0))
        ttk.Label(frame,
                  text="共用机器人时，只在一台电脑开启截图指令。同事也要用？请创建自己的飞书应用机器人，填写自己的 App ID、App Secret 和 User ID；只改 User ID 不够。",
                  foreground="#666666", wraplength=600, justify="left").pack(anchor="w", pady=(3, 0))

        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(10, 8))
        ttk.Button(controls, text="测试当前任务飞书发送", command=self.test_feishu).pack(side="left")
        ttk.Button(controls, text="清理全部截图", command=self.cleanup_screenshots).pack(side="left", padx=8)
        self.log_widget = tk.Text(frame, height=14, state="disabled", wrap="word")
        self.log_widget.pack(fill="both", expand=True)
        self.log("请先选择 mstsc 窗口，框选右上角状态框，再用“检查截图”确认窗口取图正常。")

    def _build_task_tab(self, parent, task):
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Checkbutton(top, text="启用此任务", variable=task.enabled,
                        command=lambda: self.toggle_task_enabled(task)).pack(side="left")
        ttk.Label(top, text="关闭后不监控、不发异常提醒，也不能响应远程截图。",
                  foreground="#666666").pack(side="left", padx=10)
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        task.window_box = ttk.Combobox(row, textvariable=task.window_name, state="readonly")
        task.window_box.pack(side="left", fill="x", expand=True)
        task.window_box.bind("<<ComboboxSelected>>", lambda _event, t=task: self.window_selected(t))
        ttk.Button(row, text="刷新窗口", command=self.refresh_windows).pack(side="left", padx=(8, 0))
        row2 = ttk.Frame(parent)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, textvariable=task.region_name).pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="复用其他选区", command=lambda t=task: self.copy_region(t)).pack(side="right")
        ttk.Button(row2, text="框选状态框", command=lambda t=task: self.pick_region(t)).pack(side="right", padx=8)
        ttk.Button(row2, text="检查截图", command=lambda t=task: self.check_screenshot(t)).pack(side="right")
        row3 = ttk.Frame(parent)
        row3.pack(fill="x", pady=(9, 0))
        ttk.Label(row3, text="检查间隔").pack(side="left")
        ttk.Entry(row3, textvariable=task.interval, width=7).pack(side="left", padx=8)
        ttk.Label(row3, text="秒").pack(side="left")
        task.start_btn = ttk.Button(row3, text="开始监控", command=lambda t=task: self.start(t))
        task.start_btn.pack(side="left", padx=(18, 0))
        task.stop_btn = ttk.Button(row3, text="停止", command=lambda t=task: self.stop(t), state="disabled")
        task.stop_btn.pack(side="left", padx=8)
        task.monitor_light = tk.Canvas(row3, width=24, height=24, highlightthickness=0,
                                       borderwidth=0, background="SystemButtonFace")
        task.monitor_light.pack(side="left")
        task.monitor_light_dot = task.monitor_light.create_oval(
            2, 2, 22, 22, fill="#ff3030", outline="#a00000")
        ttk.Label(row3, textvariable=task.state).pack(side="right")
        ttk.Label(parent, text="触发：起始紫→绿1→紫3；起始绿→紫1；后续绿1→紫3",
                  foreground="#555555").pack(anchor="w", pady=(6, 0))

    def active_task(self):
        index = self.task_tabs.index(self.task_tabs.select())
        return self.tasks[("A", "B", "C", "D")[index]]

    def log(self, message):
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def refresh_windows(self):
        found = list_mstsc_windows()
        self.windows = {f"{title}  [{hwnd}]": hwnd for hwnd, title in found}
        titles = self.window_titles = {hwnd: title for hwnd, title in found}
        choices = list(self.windows)
        claimed = {task.selected_hwnd for task in self.tasks.values() if task.selected_hwnd}
        for task in self.tasks.values():
            previous = task.selected_hwnd
            task.window_box.configure(values=choices)
            selected = next((name for name, hwnd in self.windows.items() if hwnd == previous), None)
            if not selected and task.window_title:
                selected = next((name for name, hwnd in self.windows.items()
                                 if titles.get(hwnd) == task.window_title and hwnd not in claimed), None)
            if not selected:
                selected = next((name for name in choices if self.windows[name] not in claimed), None)
            task.window_name.set(selected or "未找到可用的 mstsc 窗口")
            task.selected_hwnd = self.windows.get(selected)
            if task.selected_hwnd:
                claimed.add(task.selected_hwnd)
                task.window_title = titles.get(task.selected_hwnd, task.window_title)

    def window_selected(self, task):
        task.selected_hwnd = self.windows.get(task.window_name.get())
        if task.selected_hwnd:
            task.window_title = self.window_titles.get(task.selected_hwnd, task.window_title)
        try:
            self.save_config()
        except (OSError, ValueError):
            pass

    def _update_region(self, task):
        if task.region:
            x1, y1, x2, y2 = task.region
            task.region_name.set(f"状态框：窗口内 ({x1}, {y1}) · {x2-x1}×{y2-y1} 像素")
        else:
            task.region_name.set("尚未框选状态框")

    def pick_region(self, task=None):
        task = task or self.active_task()
        if not task.enabled.get():
            messagebox.showinfo("任务已关闭", f"请先启用 {task.name}。")
            return
        if not task.selected_hwnd:
            messagebox.showwarning("先选窗口", "请先打开并选择 mstsc 窗口。")
            return
        self.stop(task)
        self.root.withdraw()
        self.root.after(250, lambda: RegionPicker(
            self.root, lambda region: self.region_selected(task, region), self.restore_window))

    def restore_window(self):
        self.root.deiconify()
        self.root.lift()

    def region_selected(self, task, screen_region):
        self.restore_window()
        try:
            capture = capture_window(task.selected_hwnd)
            local = (screen_region[0] - capture.left, screen_region[1] - capture.top,
                     screen_region[2] - capture.left, screen_region[3] - capture.top)
            status, confidence = classify_status(capture.bgra, capture.width, capture.height, local)
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("无法选区", str(error))
            return
        task.region = local
        self._update_region(task)
        self.save_config()
        self.log(f"[{task.name}] 已框选状态框；当前识别为 {status}（{confidence:.0%} 采样点）。")

    def copy_region(self, task):
        source = next((item for item in self.tasks.values()
                       if item is not task and item.region), None)
        if source is None:
            messagebox.showinfo("没有可复用选区", "请先给任意其他任务框选状态框。")
            return
        task.region = tuple(source.region)
        self._update_region(task)
        self.save_config()
        self.log(f"[{task.name}] 已复用 {source.name} 的状态框选区；请用“检查截图”确认位置。")

    def toggle_task_enabled(self, task):
        if not task.enabled.get():
            self.stop(task)
            task.state.set("已关闭")
            self.log(f"[{task.name}] 已关闭，不再监控或响应远程截图。")
        else:
            task.state.set("未启动")
            self.log(f"[{task.name}] 已启用，请选择窗口并检查状态框。")
        try:
            self.save_config()
        except (OSError, ValueError) as error:
            self.log(f"任务设置保存失败：{error}")

    def save_config(self):
        tasks = {key: {"enabled": task.enabled.get(), "region": task.region,
                       "window_title": task.window_title,
                       "interval": task.interval.get().strip()}
                 for key, task in self.tasks.items()}
        data = {"tasks": tasks, "app_id": self.app_id.get().strip(),
                "chat_id": self.chats.get(self.chat_id.get().strip(), self.chat_id.get().strip()),
                "recipient_type": RECIPIENT_TYPES[self.recipient_choice.get()],
                "gif_enabled": self.gif_enabled.get(),
                "gif_default_off_applied": True,
                "authorized_id": self.authorized_id.get().strip(),
                "command_enabled": self.command_enabled.get()}
        secret = self.app_secret.get().strip()
        if secret:
            try:
                data["secret_dpapi"] = protect(secret)
            except OSError:
                # Some isolated Windows sessions cannot use DPAPI. Do not save plaintext.
                pass
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def on_gif_toggle(self):
        self.gif_enabled_value = self.gif_enabled.get()
        try:
            self.save_config()
        except (OSError, ValueError):
            pass
        self.log("随机 GIF 表情包已开启。" if self.gif_enabled_value else "随机 GIF 表情包已关闭。")

    def fetch_chats(self):
        if RECIPIENT_TYPES[self.recipient_choice.get()] != "chat_id":
            messagebox.showinfo("私聊模式", "私聊请直接在“接收地址”填写你的飞书邮箱或用户 ID。")
            return
        app_id, secret = self.app_id.get().strip(), self.app_secret.get().strip()
        if not app_id or not secret:
            messagebox.showwarning("缺少凭证", "请先填入 App ID 和 App Secret。")
            return
        self.log("正在获取机器人所在的飞书群聊。")

        def worker():
            try:
                chats = list_bot_chats(app_id, secret)
                self.events.put(("chats", chats))
            except RuntimeError as error:
                self.events.put(("chat_error", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def chat_selected(self, _event=None):
        name = self.chat_id.get()
        if name in self.chats:
            self.chat_id.set(self.chats[name])

    def check_screenshot(self, task=None):
        task = task or self.active_task()
        if not task.enabled.get():
            messagebox.showinfo("任务已关闭", f"请先启用 {task.name}。")
            return
        if not task.selected_hwnd:
            messagebox.showwarning("先选窗口", "请先打开并选择 mstsc 窗口。")
            return
        hwnd, region = task.selected_hwnd, task.region
        self.background_screenshots += 1
        self.log(f"[{task.name}] 正在捕获 mstsc 窗口，请查看打开的截图。")

        def worker():
            try:
                capture = capture_window(hwnd)
                path = save_screenshot(capture)
                if region:
                    status, confidence = classify_status(capture.bgra, capture.width, capture.height, region)
                else:
                    status, confidence = UNKNOWN, 0.0
                self.events.put(("checked", task.key, str(path), status, confidence))
            except (RuntimeError, ValueError, OSError) as error:
                self.events.put(("check_error", task.key, str(error)))
            finally:
                self.events.put(("screenshot_task_finished",))

        threading.Thread(target=worker, daemon=True).start()

    def cleanup_screenshots(self):
        if self.screenshots_busy():
            messagebox.showinfo("请稍等", "请先停止监控，并等待正在截图或发送的任务结束，再清理截图。")
            return
        paths = stored_screenshots(SCREENSHOT_DIR)
        if not paths:
            messagebox.showinfo("没有截图", "screenshots 文件夹里没有程序保存的截图。")
            return
        if not messagebox.askyesno("清理截图", f"确定删除 screenshots 文件夹里的 {len(paths)} 张程序截图吗？\n删除后无法恢复。"):
            return
        if self.screenshots_busy():
            messagebox.showinfo("请稍等", "确认期间开始了新的截图或发送任务，请等待任务结束后再清理。")
            return
        removed, failures = remove_screenshots(paths)
        self.log(f"已清理 {removed} 张截图。")
        if failures:
            messagebox.showwarning("部分截图未删除", "有些截图无法删除，请查看程序日志。")
            for failure in failures:
                self.log(f"清理失败：{failure}")

    def screenshots_busy(self):
        return bool(any(task.worker and task.worker.is_alive() for task in self.tasks.values()) or
                    self.deliveries.unfinished_tasks or self.background_screenshots or
                    (hasattr(self, "outbox") and self.outbox.has_pending_screenshot()))

    def test_feishu(self):
        task = self.active_task()
        try:
            app_id, secret, recipient, recipient_type, _ = self.validate(task)
        except ValueError as error:
            messagebox.showerror("设置有误", str(error))
            return
        hwnd = task.selected_hwnd
        self.background_screenshots += 1
        self.log(f"[{task.name}] 正在发送测试截图和文字" + ("、随机 GIF 表情包。" if self.gif_enabled_value else "。"))

        def worker():
            try:
                capture = capture_window(hwnd)
                path = save_screenshot(capture)
                token = send_completion(app_id, secret, recipient, path.read_bytes(),
                                        f"{task.name}｜监控通知测试", recipient_type)
                self.events.put(("test_sent", task.key, str(path)))
                if self.gif_enabled_value:
                    try:
                        self.send_random_gif(token, recipient, recipient_type)
                    except (RuntimeError, ValueError, OSError) as error:
                        self.events.put(("gif_error", str(error)))
            except (RuntimeError, OSError) as error:
                self.events.put(("test_error", task.key, str(error)))
            finally:
                self.events.put(("screenshot_task_finished",))

        threading.Thread(target=worker, daemon=True).start()

    def toggle_commands(self):
        if (self.command_listener and self.command_listener.thread.is_alive()
                and not self.command_listener.stopped.is_set()):
            self.stop_commands()
        elif self.command_listener and self.command_listener.thread.is_alive():
            self.log("飞书指令正在关闭，请稍等片刻再开启。")
        else:
            self.start_commands()

    def start_commands(self):
        if self.command_listener and self.command_listener.thread.is_alive():
            return
        app_id, secret = self.app_id.get().strip(), self.app_secret.get().strip()
        authorized_id = self.authorized_id.get().strip()
        if not app_id or not secret or not authorized_id:
            self.command_enabled.set(False)
            self.command_state.set("截图指令未开启")
            messagebox.showwarning("缺少设置", "请填好飞书 App ID、App Secret 和允许截图者的 User ID 或 Open ID。")
            return
        if not authorized_id.startswith("ou_") and authorized_id.startswith(("oc_", "cli_")):
            self.command_enabled.set(False)
            messagebox.showwarning("身份填写有误", "允许截图者请填写人的 User ID 或 ou_ 开头的 Open ID。")
            return
        listener = FeishuCommandListener(
            app_id, secret, authorized_id,
            lambda chat_id, task_key: self.events.put(
                ("command_capture", listener, chat_id, task_key, app_id, secret)),
            lambda message: self.events.put(("command_listener_log", listener, message)))
        self.command_listener = listener
        self.command_enabled.set(True)
        self.command_state.set("正在连接飞书")
        self.command_btn.configure(text="关闭指令")
        try:
            self.save_config()
        except (OSError, ValueError) as error:
            self.log(f"指令设置保存失败：{error}")
        listener.start()

    def stop_commands(self):
        if self.command_listener:
            self.command_listener.stop()
        self.command_enabled.set(False)
        self.command_state.set("截图指令未开启")
        self.command_btn.configure(text="开启指令")
        try:
            self.save_config()
        except (OSError, ValueError) as error:
            self.log(f"指令设置保存失败：{error}")
        self.log("飞书截图指令已关闭。")

    def capture_for_command(self, listener, chat_id, task_key, app_id, secret):
        if not self.command_enabled.get() or self.command_listener is not listener:
            return
        task = self.tasks[task_key]
        if not task.enabled.get():
            self.log(f"收到截图指令，但 {task.name} 当前已关闭。")
            threading.Thread(target=self._command_text,
                             args=(app_id, secret, chat_id, f"{task.name}当前未启用，请先在程序中启用。"),
                             daemon=True).start()
            return
        hwnd = task.selected_hwnd
        if not hwnd:
            self.log(f"收到截图指令，但 {task.name} 没有选中的 mstsc 窗口。")
            threading.Thread(target=self._command_text,
                             args=(app_id, secret, chat_id,
                                   f"{task.name}当前没有选中的 mstsc 窗口，请先在程序里选择。"),
                             daemon=True).start()
            return
        self.background_screenshots += 1
        self.log(f"[{task.name}] 收到已授权的私聊截图指令，正在截取当前 mstsc 画面。")

        def worker():
            try:
                capture = capture_window(hwnd)
                path = save_screenshot(capture)
                send_completion(app_id, secret, chat_id, path.read_bytes(),
                                f"{task.name}｜{SCREENSHOT_REPLY}", "chat_id")
                self.events.put(("command_sent", task.key, str(path)))
            except (RuntimeError, ValueError, OSError) as error:
                self.events.put(("command_error", task.key, str(error)))
                try:
                    send_text(app_id, secret, chat_id,
                              f"抱歉，{task.name}这次截图没能成功：{error}", "chat_id")
                except (RuntimeError, OSError):
                    pass
            finally:
                self.events.put(("screenshot_task_finished",))

        threading.Thread(target=worker, daemon=True).start()

    def _command_text(self, app_id, secret, chat_id, text):
        try:
            send_text(app_id, secret, chat_id, text, "chat_id")
        except (RuntimeError, OSError) as error:
            self.events.put(("command_log", f"截图指令提示发送失败：{error}"))

    def send_random_gif(self, token, recipient, recipient_type):
        path = choose_gif(GIF_DIR)
        if path is None:
            self.events.put(("gif_missing", str(GIF_DIR)))
            return
        send_gif(token, recipient, path.read_bytes(), recipient_type)
        self.events.put(("gif_sent", path.name))

    def validate(self, task=None):
        task = task or self.active_task()
        if not task.enabled.get():
            raise ValueError(f"{task.name}当前已关闭")
        if not task.selected_hwnd:
            raise ValueError("请先选择 mstsc 窗口")
        if not task.region:
            raise ValueError("请先框选右上角状态框")
        app_id = self.app_id.get().strip()
        secret = self.app_secret.get().strip()
        recipient = self.chats.get(self.chat_id.get().strip(), self.chat_id.get().strip())
        recipient_type = RECIPIENT_TYPES[self.recipient_choice.get()]
        if not app_id or not secret or not recipient:
            raise ValueError("请填好飞书 App ID、App Secret 和接收地址")
        interval = float(task.interval.get())
        if not 0.3 <= interval <= 60:
            raise ValueError("检查间隔应在 0.3 至 60 秒之间")
        return app_id, secret, recipient, recipient_type, interval

    def start(self, task=None):
        task = task or self.active_task()
        try:
            app_id, secret, recipient, recipient_type, interval = self.validate(task)
            self.save_config()
        except (ValueError, OSError) as error:
            messagebox.showerror("设置有误", str(error))
            return
        if task.worker and task.worker.is_alive():
            return
        task.stop_event = threading.Event()
        task.health = MonitorHealth(time.monotonic())
        task.monitor_interval = interval
        task.credentials = (app_id, secret, recipient, recipient_type)
        task.start_btn.configure(state="disabled")
        task.stop_btn.configure(state="normal")
        task.state.set("正在确认当前状态")
        task.worker = threading.Thread(target=self.monitor_loop,
                                       args=(task.selected_hwnd, task.region, interval, app_id, secret,
                                             recipient, recipient_type,
                                             task.stop_event, task.health, task.key), daemon=True)
        task.worker.start()
        self.set_monitor_light(task, "green")
        self.log(f"[{task.name}] 开始监控。起始紫色先等绿1→紫3；起始绿色遇紫1提醒；之后绿1→紫3再次提醒。")

    def set_monitor_light(self, task, state):
        colors = {"red": ("#ff3030", "#a00000"),
                  "green": ("#00e63a", "#00841f"),
                  "yellow": ("#ffdf21", "#a27700")}
        task.light_state = state
        task.monitor_light.itemconfigure(
            task.monitor_light_dot,
            fill=colors[state][0], outline=colors[state][1])

    def stop(self, task=None):
        task = task or self.active_task()
        if task.worker and task.worker.is_alive():
            task.stop_event.set()
            self.log(f"[{task.name}] 已停止监控。")
        task.start_btn.configure(state="normal")
        task.stop_btn.configure(state="disabled")
        task.state.set("已停止" if task.enabled.get() else "已关闭")
        task.health = None
        self.set_monitor_light(task, "red")

    def monitor_loop(self, hwnd, region, interval, app_id, secret, recipient, recipient_type,
                     stop_event, health=None, task_key="A"):
        task_name = f"{task_key}设备任务"
        latch = CompletionLatch()
        unknown_watch = UnknownFrameWatch(UNKNOWN_ALERT_SECONDS)
        pending_captures = deque()
        storage_warned = False
        overflow_warned = False
        completion_number = 0
        previous = None
        capture_failures = 0
        # Stagger concurrent tasks so their first full-window captures do not burst together.
        if stop_event.wait(0.15 * (ord(task_key) - ord("A"))):
            return
        while not stop_event.is_set():
            try:
                capture = capture_window(hwnd)
                status, confidence = classify_status(capture.bgra, capture.width, capture.height, region)
            except (RuntimeError, ValueError, OSError) as error:
                if stop_event.is_set():
                    return
                capture_failures += 1
                if health:
                    health.last_progress = time.monotonic()
                    health.capture_failures = min(capture_failures, MAX_CAPTURE_FAILURES)
                    if capture_failures == 1:
                        self.events.put(("health", health, True))
                if capture_failures == MAX_CAPTURE_FAILURES:
                    self.queue_anomaly(app_id, secret, recipient, recipient_type,
                                       f"小助手提醒：{task_name} 已连续 {MAX_CAPTURE_FAILURES} 次无法取图，监控仍在持续重试。原因：{error}。请检查远程连接；若窗口已重新打开，请手动停止并重新选择窗口。")
                    self.events.put(("capture_alert", str(error), health))
                elif capture_failures < MAX_CAPTURE_FAILURES:
                    self.events.put(("capture_retry", capture_failures, str(error), health))
                elif capture_failures % 30 == 0:
                    self.events.put(("capture_still_failing", capture_failures, str(error), health))
                stop_event.wait(interval)
                continue
            if health and stop_event.is_set():
                return
            if health:
                health.last_progress = time.monotonic()
                if health.capture_failures:
                    health.capture_failures = 0
                    self.events.put(("capture_recovered", health))
                    self.events.put(("health", health, health.warning()))
            capture_failures = 0
            if pending_captures:
                try:
                    self.save_pending_completion(pending_captures[0], app_id, secret,
                                                 recipient, recipient_type, task_key)
                    pending_captures.popleft()
                    if not pending_captures:
                        storage_warned = overflow_warned = False
                        if health and health.storage_warning:
                            health.storage_warning = False
                            self.events.put(("health", health, health.warning()))
                    self.events.put(("pending_saved",))
                except (OSError, ValueError) as error:
                    if not storage_warned:
                        storage_warned = True
                        self.queue_anomaly(app_id, secret, recipient, recipient_type,
                                           f"小助手提醒：{task_name} 的测试结束截图暂时无法保存或加入待发队列：{error}。监控仍在继续，会尝试补存截图；请检查磁盘空间。")
                        self.events.put(("storage_retry", str(error), health))
                    if health and not health.storage_warning:
                        health.storage_warning = True
                        self.events.put(("health", health, True))
            if health and stop_event.is_set():
                return
            if status != previous:
                self.events.put(("status", status, confidence, health, task_key))
                previous = status
            if unknown_watch.observe(status, time.monotonic()):
                self.queue_anomaly(app_id, secret, recipient, recipient_type,
                                   f"小助手提醒：{task_name} 画面已连续至少 30 秒无法识别 Testing 或 credence，可能漏掉测试结束。监控仍在运行，请检查远程画面和截图。")
            if health and health.unknown_warned != unknown_watch.warned:
                health.unknown_warned = unknown_watch.warned
                self.events.put(("health", health, health.warning()))
            old_phase = latch.phase
            completed = latch.observe(status)
            if old_phase != latch.phase and latch.phase in ("wait_purple", "wait_first_purple"):
                self.events.put(("rearmed", task_key))
            if completed:
                message = f"{task_name}｜{message_for(completion_number)}"
                completion_number += 1
                if len(pending_captures) < MAX_PENDING_CAPTURES:
                    pending_captures.append({"capture": capture, "message": message,
                                             "captured_at": datetime.now(), "path": None})
                    if len(pending_captures) == 1:
                        try:
                            self.save_pending_completion(pending_captures[0], app_id, secret,
                                                         recipient, recipient_type, task_key)
                            pending_captures.popleft()
                            storage_warned = overflow_warned = False
                            if health and health.storage_warning:
                                health.storage_warning = False
                                self.events.put(("health", health, health.warning()))
                        except (OSError, ValueError) as error:
                            if not storage_warned:
                                storage_warned = True
                                self.queue_anomaly(app_id, secret, recipient, recipient_type,
                                                   f"小助手提醒：{task_name} 的测试结束截图暂时无法保存或加入待发队列：{error}。监控仍在继续，会尝试补存截图；请检查磁盘空间。")
                                self.events.put(("storage_retry", str(error), health))
                            if health and not health.storage_warning:
                                health.storage_warning = True
                                self.events.put(("health", health, True))
                elif not overflow_warned:
                    overflow_warned = True
                    self.queue_anomaly(app_id, secret, recipient, recipient_type,
                                       f"小助手提醒：{task_name} 待保存测试截图已达到 {MAX_PENDING_CAPTURES} 张，后续测试结束画面可能漏存。监控仍在继续，请尽快检查磁盘空间。")
            stop_event.wait(interval)

    def save_pending_completion(self, pending, app_id, secret, recipient, recipient_type,
                                task_key="A"):
        if pending["path"] is None:
            pending["path"] = save_screenshot(pending["capture"], pending["captured_at"])
            self.events.put(("captured", task_key, str(pending["path"])))
        task_id = self.outbox.create(pending["path"], app_id, recipient,
                                     recipient_type, pending["message"],
                                     f"{task_key}设备任务｜{capture_time_text(pending['captured_at'])}")
        self.deliveries.put((task_id, secret))

    def check_monitor_health(self):
        for task in self.tasks.values():
            health = task.health
            if not health or task.stop_event.is_set():
                continue
            if task.worker and not task.worker.is_alive():
                if not health.worker_death_notified:
                    health.worker_death_notified = True
                    task.start_btn.configure(state="normal")
                    task.state.set("监控线程异常退出")
                    self.queue_anomaly(*task.credentials,
                                       f"小助手提醒：{task.name} 监控线程意外退出，当前没有继续检测画面。请检查程序并重新开始监控。")
            elif time.monotonic() - health.last_progress > watchdog_timeout(task.monitor_interval):
                health.watchdog_warned = True
                if not health.watchdog_notified:
                    health.watchdog_notified = True
                    self.queue_anomaly(*task.credentials,
                                       f"小助手提醒：{task.name} 监控长时间没有完成一次取图，可能已经卡住。请查看远程画面和监控程序。")
            elif health.watchdog_warned:
                health.watchdog_warned = False
                health.watchdog_notified = False
            desired = "yellow" if health.warning() else "green"
            if task.light_state != desired:
                self.set_monitor_light(task, desired)
                if desired == "yellow":
                    self.log(f"[{task.name}] 监控健康警示：画面读取或监控线程出现异常，请查看日志。")
                    self.root.bell()
                else:
                    self.log(f"[{task.name}] 监控画面已恢复正常读取。")
        self.root.after(1000, self.check_monitor_health)

    def queue_anomaly(self, app_id, secret, recipient, recipient_type, message):
        self.events.put(("anomaly", message))
        try:
            task_id = self.outbox.create_alert(app_id, recipient, recipient_type, message)
            self.deliveries.put((task_id, secret))
        except (OSError, ValueError) as error:
            self.events.put(("anomaly_send_error", f"无法保存待发送提醒：{error}"))
            threading.Thread(target=self.send_unstored_anomaly,
                             args=(app_id, secret, recipient, recipient_type, message),
                             daemon=True).start()

    def send_unstored_anomaly(self, app_id, secret, recipient, recipient_type, message):
        try:
            send_text(app_id, secret, recipient, message, recipient_type)
            self.events.put(("anomaly_sent",))
        except (RuntimeError, ValueError, OSError) as error:
            self.events.put(("anomaly_send_error", f"临时直接发送也失败：{error}"))

    def delivery_loop(self):
        while not self.shutdown_event.is_set():
            try:
                task_id, secret = self.deliveries.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.deliver_task(task_id, secret)
                self.delivery_attempts.pop(task_id, None)
            except (RuntimeError, ValueError, OSError) as error:
                attempt = self.delivery_attempts.get(task_id, 0) + 1
                self.delivery_attempts[task_id] = attempt
                delay = min(3 * (2 ** min(attempt - 1, 5)), 60)
                self.events.put(("send_retry", str(error), delay))
                timer = threading.Timer(delay, self.retry_delivery, args=(task_id, secret))
                timer.daemon = True
                timer.start()
            finally:
                self.deliveries.task_done()

    def retry_delivery(self, task_id, secret):
        if not self.shutdown_event.is_set():
            self.deliveries.put((task_id, secret))

    def delivery_secret(self, task, secret):
        if secret:
            return secret
        saved = load_config()
        if saved.get("app_id") != task["app_id"] or not saved.get("secret_dpapi"):
            raise RuntimeError("补发提醒需要原飞书 App ID 和 App Secret；请检查程序设置")
        try:
            return unprotect(saved["secret_dpapi"])
        except (OSError, ValueError) as error:
            raise RuntimeError(f"无法读取补发提醒所需的 App Secret：{error}") from error

    def deliver_task(self, task_id, secret):
        task = self.outbox.load(task_id)
        if task["kind"] == "completion" and "time_sent" not in task:
            task["capture_time"] = (capture_time_from_filename(task["screenshot"])
                                    or capture_time_text(datetime.now()))
            task["time_uuid"] = uuid.uuid4().hex
            task["time_sent"] = bool(task["text_sent"])
            self.outbox.save(task)
        token = get_tenant_token(task["app_id"], self.delivery_secret(task, secret))
        if task["kind"] == "completion":
            if not task["image_key"]:
                task["image_key"] = upload_image(token, self.outbox.screenshot_path(task).read_bytes())
                self.outbox.save(task)
            if not task["image_sent"]:
                send_image_key(token, task["recipient"], task["recipient_type"],
                               task["image_key"], task["image_uuid"])
                task["image_sent"] = True
                self.outbox.save(task)
            if not task["time_sent"]:
                send_text_token(token, task["recipient"], task["recipient_type"],
                                task["capture_time"], task["time_uuid"])
                task["time_sent"] = True
                self.outbox.save(task)
                self.events.put(("capture_time_sent", task["capture_time"]))
        if not task["text_sent"]:
            send_text_token(token, task["recipient"], task["recipient_type"],
                            task["message"], task["text_uuid"])
            task["text_sent"] = True
            self.outbox.save(task)
        self.outbox.finish(task_id)
        if task["kind"] == "alert":
            self.events.put(("anomaly_sent",))
        else:
            self.events.put(("sent", task["message"]))
            if self.gif_enabled_value:
                try:
                    self.send_random_gif(token, task["recipient"], task["recipient_type"])
                except (RuntimeError, ValueError, OSError) as error:
                    self.events.put(("gif_error", str(error)))

    def process_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "status":
                task = self.tasks[event[4]]
                if event[3] is not None and event[3] is not task.health:
                    continue
                status, confidence = event[1:3]
                label = {GREEN: "绿色 Testing", PURPLE: "紫色 credence", UNKNOWN: "未知画面"}[status]
                task.state.set(label)
                self.log(f"[{task.name}] 观察到 {label}（{confidence:.0%} 采样点）。")
            elif kind == "health":
                task = next((item for item in self.tasks.values() if item.health is event[1]), None)
                if task and not task.stop_event.is_set():
                    desired = "yellow" if event[2] else "green"
                    if task.light_state != desired:
                        self.set_monitor_light(task, desired)
                        if desired == "yellow":
                            self.log(f"[{task.name}] 监控健康警示：正在检查异常画面，请查看日志。")
                            self.root.bell()
                        else:
                            self.log(f"[{task.name}] 监控画面已恢复正常读取。")
            elif kind == "capture_retry":
                task = next((item for item in self.tasks.values() if item.health is event[3]), None)
                if task:
                    self.log(f"[{task.name}] 取图暂时失败（{event[1]}/{MAX_CAPTURE_FAILURES}），继续重试：{event[2]}")
            elif kind == "capture_alert":
                task = next((item for item in self.tasks.values() if item.health is event[2]), None)
                if task:
                    self.log(f"[{task.name}] 连续 {MAX_CAPTURE_FAILURES} 次取图失败，已提醒；监控保持运行并继续重试：{event[1]}")
            elif kind == "capture_still_failing":
                task = next((item for item in self.tasks.values() if item.health is event[3]), None)
                if task:
                    self.log(f"[{task.name}] 取图仍未恢复（连续 {event[1]} 次）：{event[2]}")
            elif kind == "capture_recovered":
                task = next((item for item in self.tasks.values() if item.health is event[1]), None)
                if task:
                    self.log(f"[{task.name}] 取图已恢复，继续监控。")
            elif kind == "storage_retry":
                task = next((item for item in self.tasks.values() if item.health is event[2]), None)
                if task:
                    self.log(f"[{task.name}] 测试截图暂时无法保存，已留在内存中继续尝试：{event[1]}")
            elif kind == "pending_saved":
                self.log("待保存的测试截图已补存并加入飞书发送队列。")
            elif kind == "captured":
                self.log(f"[{self.tasks[event[1]].name}] 测试已结束；截图已保存：{event[2]}")
            elif kind == "anomaly":
                self.log(event[1])
                self.root.bell()
            elif kind == "anomaly_sent":
                self.log("监控异常提醒已发送到飞书。")
            elif kind == "anomaly_send_error":
                self.log(f"监控异常提醒未能发送到飞书：{event[1]}")
            elif kind == "rearmed":
                self.log(f"[{self.tasks[event[1]].name}] 已看到绿色 Testing，等待紫色 credence 触发本轮提醒。")
            elif kind == "sent":
                self.log(f"截图、截图时间和提醒已发送到飞书：{event[1]}")
            elif kind == "capture_time_sent":
                self.log(f"截图时间已发送到飞书：{event[1]}")
            elif kind == "send_error":
                self.log(f"截图已保存在本机，但飞书发送失败：{event[1]}")
            elif kind == "send_retry":
                self.log(f"飞书发送暂时失败：{event[1]}；{event[2]} 秒后自动补发。")
            elif kind == "gif_sent":
                self.log(f"随机 GIF 表情包已发送：{event[1]}")
            elif kind == "gif_missing":
                self.log(f"未找到可用 GIF 表情包：{event[1]}")
            elif kind == "gif_error":
                self.log(f"截图和文字已发送，但 GIF 表情包发送失败：{event[1]}")
            elif kind == "chats":
                chats = event[1]
                self.chats = {f"{name}  [{chat_id}]": chat_id for chat_id, name in chats}
                self.chat_box.configure(values=list(self.chats))
                self.log(f"找到 {len(chats)} 个群聊；请选接收测试提醒的群。")
                if not chats:
                    self.log("如果群列表为空，请先把应用机器人加入飞书群并检查应用权限。")
            elif kind == "chat_error":
                self.log(f"获取群聊失败：{event[1]}")
            elif kind == "checked":
                task_key, path, status, confidence = event[1:]
                self.log(f"[{self.tasks[task_key].name}] 窗口截图：{Path(path).name}；状态框：{status}（{confidence:.0%} 采样点）。")
                try:
                    os.startfile(path)
                except OSError as error:
                    self.log(f"无法自动打开截图：{error}")
            elif kind == "check_error":
                self.log(f"[{self.tasks[event[1]].name}] 窗口截图失败：{event[2]}")
            elif kind == "test_sent":
                self.log(f"[{self.tasks[event[1]].name}] 测试截图和“监控通知测试”已发送到飞书。")
            elif kind == "test_error":
                self.log(f"[{self.tasks[event[1]].name}] 飞书测试发送失败：{event[2]}")
            elif kind == "command_capture":
                self.capture_for_command(*event[1:])
            elif kind == "command_sent":
                self.log(f"[{self.tasks[event[1]].name}] 飞书指令截图已发送：{event[2]}")
            elif kind == "command_error":
                self.log(f"[{self.tasks[event[1]].name}] 飞书指令截图失败：{event[2]}")
            elif kind == "command_log":
                self.log(event[1])
            elif kind == "command_listener_log":
                listener, message = event[1:]
                if listener is not self.command_listener or not self.command_enabled.get():
                    continue
                self.log(message)
                if "已连接" in message:
                    self.command_state.set("飞书指令已连接")
                elif "连接失败" in message or "缺少" in message:
                    self.command_state.set("连接异常")
            elif kind == "screenshot_task_finished":
                self.background_screenshots = max(0, self.background_screenshots - 1)
        self.root.after(100, self.process_events)

    def close(self):
        for task in self.tasks.values():
            task.stop_event.set()
        self.shutdown_event.set()
        if self.command_listener:
            self.command_listener.stop()
        try:
            self.save_config()
        except (OSError, ValueError):
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    root.iconbitmap(str(Path(__file__).resolve().parent / "sikadi.ico"))
    App(root)
    root.mainloop()
