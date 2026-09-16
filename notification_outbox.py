"""Durable, stage-aware Feishu completion notifications."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path


def capture_time_text(captured_at: datetime) -> str:
    return (f"{captured_at.year}年{captured_at.month}月{captured_at.day}日 "
            f"{captured_at.hour:02d}:{captured_at.minute:02d}:{captured_at.second:02d}")


def capture_time_from_filename(name: str) -> str | None:
    match = re.fullmatch(r"completion_(\d{8})_(\d{6})_\d{6}\.png", name)
    if not match:
        return None
    try:
        captured_at = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return capture_time_text(captured_at)


class NotificationOutbox:
    def __init__(self, folder: Path, screenshots: Path):
        self.folder = folder
        self.screenshots = screenshots

    def create(self, screenshot: Path, app_id: str, recipient: str,
               recipient_type: str, message: str, capture_time: str | None = None) -> str:
        if screenshot.parent.resolve() != self.screenshots.resolve():
            raise ValueError("待发送截图不在程序截图目录")
        task_id = uuid.uuid4().hex
        task = {"id": task_id, "kind": "completion", "app_id": app_id, "recipient": recipient,
                "recipient_type": recipient_type, "screenshot": screenshot.name,
                "capture_time": (capture_time or capture_time_from_filename(screenshot.name)
                                 or capture_time_text(datetime.now())),
                "message": message, "image_key": "", "image_sent": False,
                "time_sent": False, "time_uuid": uuid.uuid4().hex,
                "text_sent": False, "image_uuid": uuid.uuid4().hex,
                "text_uuid": uuid.uuid4().hex}
        self.save(task)
        return task_id

    def create_alert(self, app_id: str, recipient: str,
                     recipient_type: str, message: str) -> str:
        task_id = uuid.uuid4().hex
        task = {"id": task_id, "kind": "alert", "app_id": app_id,
                "recipient": recipient, "recipient_type": recipient_type,
                "message": message, "text_sent": False,
                "text_uuid": uuid.uuid4().hex}
        self.save(task)
        return task_id

    def path(self, task_id: str) -> Path:
        if not task_id or any(ch not in "0123456789abcdef" for ch in task_id):
            raise ValueError("待发送任务编号无效")
        return self.folder / f"{task_id}.json"

    def save(self, task: dict) -> None:
        self.folder.mkdir(exist_ok=True)
        target = self.path(task["id"])
        temporary = target.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(task, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, task_id: str) -> dict:
        task = json.loads(self.path(task_id).read_text(encoding="utf-8"))
        screenshot = task.get("screenshot", "")
        if (task.get("id") != task_id or task.get("kind") not in ("completion", "alert")
                or (task["kind"] == "completion" and
                    (not screenshot.startswith("completion_")
                     or not screenshot.endswith(".png")
                     or Path(screenshot).name != screenshot))):
            raise ValueError("待发送任务内容无效")
        return task

    def pending_ids(self) -> list[str]:
        if not self.folder.is_dir():
            return []
        return sorted(path.stem for path in self.folder.glob("*.json")
                      if path.is_file() and not path.is_symlink())

    def has_pending_screenshot(self) -> bool:
        for task_id in self.pending_ids():
            try:
                if self.load(task_id)["kind"] == "completion":
                    return True
            except (OSError, ValueError):
                return True
        return False

    def screenshot_path(self, task: dict) -> Path:
        return self.screenshots / task["screenshot"]

    def finish(self, task_id: str) -> None:
        self.path(task_id).unlink()
