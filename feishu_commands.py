"""Small, strict policy for Feishu private-chat screenshot commands."""

from __future__ import annotations

from collections import deque


TASK_KEYS = ("A", "B", "C", "D")
UNAUTHORIZED_REPLY = "此指令未授权"
SCREENSHOT_REPLY = "这是你要的 mstsc 当前截图，我已经帮你送过来了。"


def command_task(text: str) -> str | None:
    """Return A-D for 截图A/jta style commands; Latin letters ignore case."""
    normalized = text.strip().casefold()
    for key in TASK_KEYS:
        suffix = key.casefold()
        if normalized in (f"截图{suffix}", f"jt{suffix}"):
            return key
    return None


def screenshot_request(message, authorized_id: str) -> tuple[str, str] | None:
    """Return (authorization decision, task key) for a private screenshot command."""
    task_key = command_task(message.content_text) if message.raw_content_type == "text" else None
    if (message.chat_type != "p2p" or message.raw_content_type != "text"
            or task_key is None
            or message.sender_type != "user" or not message.chat_id):
        return None
    sender = message.sender
    if authorized_id.startswith("ou_"):
        actual = sender.open_id
    else:
        actual = sender.user_id
    return ("authorized" if actual and actual == authorized_id else "unauthorized", task_key)


class SeenCommands:
    """Prevent repeated delivery of a command after reconnect or event retries."""

    def __init__(self, limit: int = 1024):
        self.limit = limit
        self.order = deque()
        self.ids = set()

    def add(self, message_id: str) -> bool:
        if not message_id or message_id in self.ids:
            return False
        self.order.append(message_id)
        self.ids.add(message_id)
        if len(self.order) > self.limit:
            self.ids.remove(self.order.popleft())
        return True
