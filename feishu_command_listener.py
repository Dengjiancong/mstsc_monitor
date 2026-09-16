"""Receive private Feishu commands over an outbound WebSocket connection."""

from __future__ import annotations

import asyncio
import threading

from feishu_client import send_text
from feishu_commands import SeenCommands, UNAUTHORIZED_REPLY, screenshot_request


class FeishuCommandListener:
    def __init__(self, app_id: str, app_secret: str, authorized_id: str,
                 request_capture, report):
        self.app_id, self.app_secret = app_id, app_secret
        self.authorized_id = authorized_id
        self.request_capture = request_capture
        self.report = report
        self.stopped = threading.Event()
        self.seen = SeenCommands()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()

    def _run(self):
        try:
            from lark_channel import FeishuChannel
        except ImportError:
            self.report("指令接收未启动：缺少 lark-channel-sdk。请运行“安装飞书指令依赖.bat”。")
            return
        while not self.stopped.is_set():
            try:
                asyncio.run(self._session(FeishuChannel))
                return
            except Exception as error:
                if not self.stopped.is_set():
                    self.report(f"飞书指令连接失败：{error}；10 秒后重试。")
                    self.stopped.wait(10)

    async def _session(self, channel_class):
        channel = channel_class(app_id=self.app_id, app_secret=self.app_secret)

        async def reply_unauthorized(chat_id):
            try:
                await asyncio.to_thread(send_text, self.app_id, self.app_secret,
                                        chat_id, UNAUTHORIZED_REPLY, "chat_id")
                self.report("收到未授权的截图指令，已回复“此指令未授权”。")
            except (RuntimeError, OSError) as error:
                self.report(f"未授权指令的回复发送失败：{error}")

        def on_message(message):
            if self.stopped.is_set():
                return
            request = screenshot_request(message, self.authorized_id)
            if request is None or not self.seen.add(message.message_id):
                return
            decision, task_key = request
            if decision == "authorized":
                self.request_capture(message.chat_id, task_key)
            else:
                asyncio.create_task(reply_unauthorized(message.chat_id))

        channel.on("message", on_message)
        channel.on("reconnecting", lambda: self.report("飞书指令连接中断，正在重连。"))
        channel.on("reconnected", lambda: self.report("飞书指令连接已恢复。"))
        await channel.connect_until_ready(timeout=30)
        self.report("飞书截图指令已连接；可发送“截图A”或“jta”（A-D，不区分大小写）。")
        try:
            while not self.stopped.is_set():
                await asyncio.sleep(0.25)
        finally:
            await channel.disconnect()
