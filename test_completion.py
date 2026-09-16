import asyncio
import json
from types import SimpleNamespace
import queue
import struct
import tempfile
import threading
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from completion_core import CompletionLatch, UnknownFrameWatch, classify_status
from completion_messages import MESSAGES, message_for
from gif_picker import choose_gif, valid_gifs
from win_capture import CapturedWindow
from completion_monitor import App, stored_screenshots
from monitor_health import MonitorHealth, watchdog_timeout
from notification_outbox import (NotificationOutbox, capture_time_from_filename,
                                 capture_time_text)
from feishu_commands import SeenCommands, screenshot_request
from feishu_command_listener import FeishuCommandListener
import feishu_client


class CompletionTests(unittest.TestCase):
    def test_initial_purple_then_green_then_purple(self):
        latch = CompletionLatch()
        self.assertFalse(latch.observe("credence"))
        self.assertFalse(latch.observe("credence"))
        self.assertFalse(any(latch.observe("credence") for _ in range(5)))
        self.assertFalse(latch.observe("testing"))
        self.assertFalse(latch.observe("credence"))
        self.assertFalse(latch.observe("credence"))
        self.assertTrue(latch.observe("credence"))
        self.assertFalse(any(latch.observe("credence") for _ in range(5)))

    def test_initial_green_then_purple(self):
        latch = CompletionLatch()
        self.assertFalse(latch.observe("testing"))
        self.assertTrue(latch.observe("credence"))
        self.assertFalse(any(latch.observe("credence") for _ in range(5)))
        self.assertFalse(latch.observe("testing"))
        self.assertFalse(latch.observe("credence"))
        self.assertFalse(latch.observe("credence"))
        self.assertTrue(latch.observe("credence"))

    def test_detects_one_completion_per_green_cycle(self):
        latch = CompletionLatch()
        sequence = ["credence"] * 4 + ["testing"] + ["credence"] * 8 \
                   + ["testing"] + ["credence"] * 3
        self.assertEqual(sum(latch.observe(state) for state in sequence), 2)
        self.assertFalse(latch.observe("credence"))
        self.assertFalse(latch.observe("testing"))
        self.assertFalse(latch.observe("credence"))
        self.assertFalse(latch.observe("credence"))
        self.assertTrue(latch.observe("credence"))

    def test_monitor_continues_sampling_while_notifications_are_queued(self):
        states = ["credence"] * 3 + ["testing"] + ["credence"] * 3 \
                 + ["testing"] + ["credence"] * 3
        stop_event = threading.Event()
        app = object.__new__(App)
        app.events = queue.Queue()
        app.deliveries = queue.Queue()
        app.gif_enabled_value = False
        capture = CapturedWindow(2, 1, b"\0" * 8, 0, 0)

        def classify(*_args):
            status = states.pop(0)
            if not states:
                stop_event.set()
            return status, 1.0

        with tempfile.TemporaryDirectory() as temporary, \
             patch("completion_monitor.capture_window", return_value=capture), \
             patch("completion_monitor.classify_status", side_effect=classify), \
             patch("completion_monitor.save_screenshot",
                   return_value=Path(temporary) / "screenshots" / "completion_test.png"), \
             patch("completion_monitor.send_completion") as sender:
            app.outbox = NotificationOutbox(Path(temporary) / "pending",
                                            Path(temporary) / "screenshots")
            app.monitor_loop(1, (0, 0, 1, 1), 0, "app", "secret", "recipient", "user_id", stop_event)

        self.assertEqual(app.deliveries.qsize(), 2)
        self.assertEqual(len(states), 0)
        sender.assert_not_called()

    def test_unknown_frame_alerts_once_until_picture_recovers(self):
        watch = UnknownFrameWatch(30)
        self.assertFalse(watch.observe("unknown", 100))
        self.assertFalse(watch.observe("unknown", 129))
        self.assertTrue(watch.observe("unknown", 130))
        self.assertFalse(watch.observe("unknown", 180))
        self.assertFalse(watch.observe("testing", 181))
        self.assertFalse(watch.observe("unknown", 200))
        self.assertTrue(watch.observe("unknown", 230))

    def test_closed_window_warns_after_three_failures_but_keeps_retrying(self):
        app = object.__new__(App)
        app.events = queue.Queue()
        app.deliveries = queue.Queue()
        stop_event = threading.Event()
        with tempfile.TemporaryDirectory() as temporary, \
             patch("completion_monitor.capture_window",
                   side_effect=RuntimeError("mstsc 窗口已关闭")) as capture, \
             patch("completion_monitor.user32.IsWindow", return_value=False), \
             patch.object(stop_event, "wait",
                          side_effect=lambda _interval: stop_event.set()
                          if capture.call_count >= 4 else None):
            app.outbox = NotificationOutbox(Path(temporary) / "pending",
                                            Path(temporary) / "screenshots")
            app.monitor_loop(1, (0, 0, 1, 1), 0, "app", "secret", "recipient", "user_id",
                             stop_event)
            notification = app.outbox.load(app.deliveries.get_nowait()[0])
        self.assertIn("mstsc 窗口已关闭", notification["message"])
        self.assertIn("仍在持续重试", notification["message"])
        self.assertEqual(notification["kind"], "alert")
        self.assertEqual(capture.call_count, 4)
        self.assertEqual([event[0] for event in list(app.events.queue)].count("anomaly"), 1)
        self.assertNotIn("error", [event[0] for event in list(app.events.queue)])

    def test_transient_capture_failure_recovers_without_stopping(self):
        app = object.__new__(App)
        app.events = queue.Queue()
        app.deliveries = queue.Queue()
        stop_event = threading.Event()
        health = MonitorHealth(0)
        capture = CapturedWindow(2, 1, b"\0" * 8, 0, 0)

        with tempfile.TemporaryDirectory() as temporary, \
             patch("completion_monitor.capture_window",
                   side_effect=[RuntimeError("暂时无法取图"), capture]) as capture_mock, \
             patch("completion_monitor.user32.IsWindow", return_value=True), \
             patch("completion_monitor.user32.IsIconic", return_value=False), \
             patch("completion_monitor.classify_status", return_value=("testing", 1.0)), \
             patch.object(stop_event, "wait",
                          side_effect=lambda _interval: stop_event.set()
                          if capture_mock.call_count >= 2 else None):
            app.outbox = NotificationOutbox(Path(temporary) / "pending",
                                            Path(temporary) / "screenshots")
            app.monitor_loop(1, (0, 0, 1, 1), 0, "app", "secret", "recipient",
                             "user_id", stop_event, health)
            self.assertEqual(app.outbox.pending_ids(), [])
        self.assertEqual(health.capture_failures, 0)
        kinds = [event[0] for event in list(app.events.queue)]
        self.assertIn("capture_retry", kinds)
        self.assertIn("capture_recovered", kinds)
        self.assertNotIn("error", kinds)

    def test_three_consecutive_capture_failures_warn_once_and_keep_running(self):
        app = object.__new__(App)
        app.events = queue.Queue()
        app.deliveries = queue.Queue()
        health = MonitorHealth(0)
        stop_event = threading.Event()
        with tempfile.TemporaryDirectory() as temporary, \
             patch("completion_monitor.capture_window",
                   side_effect=RuntimeError("取图暂时失败")) as capture, \
             patch("completion_monitor.user32.IsWindow", return_value=True), \
             patch.object(stop_event, "wait",
                          side_effect=lambda _interval: stop_event.set()
                          if capture.call_count >= 5 else None):
            app.outbox = NotificationOutbox(Path(temporary) / "pending",
                                            Path(temporary) / "screenshots")
            app.monitor_loop(1, (0, 0, 1, 1), 0, "app", "secret", "recipient",
                             "user_id", stop_event, health)
            self.assertEqual(len(app.outbox.pending_ids()), 1)
        self.assertEqual(capture.call_count, 5)
        self.assertEqual(health.capture_failures, 3)
        kinds = [event[0] for event in list(app.events.queue)]
        self.assertEqual(kinds.count("capture_retry"), 2)
        self.assertEqual(kinds.count("capture_alert"), 1)
        self.assertEqual(kinds.count("anomaly"), 1)
        self.assertNotIn("error", kinds)

    def test_three_failed_captures_then_recovery_restores_health(self):
        app = object.__new__(App)
        app.events = queue.Queue()
        app.deliveries = queue.Queue()
        health = MonitorHealth(0)
        stop_event = threading.Event()
        capture = CapturedWindow(2, 1, b"\0" * 8, 0, 0)
        with tempfile.TemporaryDirectory() as temporary, \
             patch("completion_monitor.capture_window",
                   side_effect=[RuntimeError("暂时无法取图")] * 3 + [capture]) as capturer, \
             patch("completion_monitor.user32.IsWindow", return_value=True), \
             patch("completion_monitor.classify_status", return_value=("testing", 1.0)), \
             patch.object(stop_event, "wait",
                          side_effect=lambda _interval: stop_event.set()
                          if capturer.call_count >= 4 else None):
            app.outbox = NotificationOutbox(Path(temporary) / "pending",
                                            Path(temporary) / "screenshots")
            app.monitor_loop(1, (0, 0, 1, 1), 0, "app", "secret", "recipient",
                             "user_id", stop_event, health)
        self.assertEqual(health.capture_failures, 0)
        kinds = [event[0] for event in list(app.events.queue)]
        self.assertEqual(kinds.count("anomaly"), 1)
        self.assertIn("capture_recovered", kinds)
        self.assertNotIn("error", kinds)

    def test_screenshot_save_failure_is_retried_without_stopping_monitor(self):
        app = object.__new__(App)
        app.events = queue.Queue()
        app.deliveries = queue.Queue()
        health = MonitorHealth(0)
        stop_event = threading.Event()
        capture = CapturedWindow(2, 1, b"\0" * 8, 0, 0)
        statuses = ["testing", "credence", "credence"]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            shots = folder / "screenshots"
            shots.mkdir()
            screenshot = shots / "completion_recovered.png"

            def save(_capture, _captured_at):
                self.assertEqual(_captured_at, datetime(2026, 9, 16, 8, 24, 52, 702754))
                if not screenshot.exists():
                    screenshot.write_bytes(b"PNG")
                    raise OSError("磁盘暂时不可写")
                return screenshot

            def classify(*_args):
                result = statuses.pop(0)
                return result, 1.0

            app.outbox = NotificationOutbox(folder / "pending", shots)
            with patch("completion_monitor.capture_window", return_value=capture), \
                 patch("completion_monitor.classify_status", side_effect=classify), \
                 patch("completion_monitor.save_screenshot", side_effect=save), \
                 patch("completion_monitor.datetime") as clock, \
                 patch.object(stop_event, "wait",
                              side_effect=lambda _interval: stop_event.set()
                              if not statuses else None):
                clock.now.return_value = datetime(2026, 9, 16, 8, 24, 52, 702754)
                app.monitor_loop(1, (0, 0, 1, 1), 0, "app", "secret", "recipient",
                                 "user_id", stop_event, health)
            tasks = [app.outbox.load(task_id) for task_id in app.outbox.pending_ids()]
            kinds = [task["kind"] for task in tasks]
            self.assertIn("completion", kinds)
            self.assertIn("alert", kinds)
            self.assertEqual(next(task for task in tasks if task["kind"] == "completion")
                             ["capture_time"], "2026年9月16日 08:24:52")
        self.assertFalse(health.storage_warning)
        event_kinds = [event[0] for event in list(app.events.queue)]
        self.assertIn("storage_retry", event_kinds)
        self.assertIn("pending_saved", event_kinds)
        self.assertNotIn("error", event_kinds)

    def test_pending_delivery_resumes_at_text_without_resending_image(self):
        app = object.__new__(App)
        app.events = queue.Queue()
        app.gif_enabled_value = False
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            shots = folder / "screenshots"
            shots.mkdir()
            screenshot = shots / "completion_20260916_082452_702754.png"
            screenshot.write_bytes(b"PNG")
            app.outbox = NotificationOutbox(folder / "pending", shots)
            task_id = app.outbox.create(screenshot, "app", "mine", "user_id", "测试结束")
            with patch("completion_monitor.get_tenant_token", return_value="token"), \
                 patch("completion_monitor.upload_image", return_value="img_key"), \
                 patch("completion_monitor.send_image_key") as image_sender, \
                 patch("completion_monitor.send_text_token",
                       side_effect=[RuntimeError("断网"), None, None]) as text_sender:
                with self.assertRaises(RuntimeError):
                    app.deliver_task(task_id, "secret")
                self.assertTrue(app.outbox.load(task_id)["image_sent"])
                self.assertEqual(NotificationOutbox(folder / "pending", shots).pending_ids(),
                                 [task_id])
                app.deliver_task(task_id, "secret")
            self.assertEqual(image_sender.call_count, 1)
            self.assertEqual(text_sender.call_count, 3)
            self.assertEqual(text_sender.call_args_list[0].args[-1],
                             text_sender.call_args_list[1].args[-1])
            self.assertEqual(text_sender.call_args_list[0].args[-2],
                             "2026年9月16日 08:24:52")
            self.assertEqual(text_sender.call_args_list[2].args[-2], "测试结束")
            self.assertEqual(app.outbox.pending_ids(), [])

    def test_completion_sends_screenshot_time_reminder_then_optional_gif(self):
        app = object.__new__(App)
        app.events = queue.Queue()
        app.gif_enabled_value = True
        sent = []
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            shots = folder / "screenshots"
            shots.mkdir()
            screenshot = shots / "completion_20260916_210600_000001.png"
            screenshot.write_bytes(b"PNG")
            app.outbox = NotificationOutbox(folder / "pending", shots)
            task_id = app.outbox.create(screenshot, "app", "mine", "user_id", "温柔提醒")
            app.send_random_gif = lambda *_args: sent.append("gif")
            with patch("completion_monitor.get_tenant_token", return_value="token"), \
                 patch("completion_monitor.upload_image", side_effect=lambda *_args: sent.append("upload") or "img"), \
                 patch("completion_monitor.send_image_key", side_effect=lambda *_args: sent.append("image")), \
                 patch("completion_monitor.send_text_token",
                       side_effect=lambda _token, _recipient, _type, message, _uuid: sent.append(message)):
                app.deliver_task(task_id, "secret")
            self.assertEqual(app.outbox.pending_ids(), [])
        self.assertEqual(sent, ["upload", "image", "2026年9月16日 21:06:00",
                                "温柔提醒", "gif"])

    def test_reminder_failure_retries_only_reminder_after_time_was_sent(self):
        app = object.__new__(App)
        app.events = queue.Queue()
        app.gif_enabled_value = False
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            shots = folder / "screenshots"
            shots.mkdir()
            screenshot = shots / "completion_20260916_082452_702754.png"
            screenshot.write_bytes(b"PNG")
            app.outbox = NotificationOutbox(folder / "pending", shots)
            task_id = app.outbox.create(screenshot, "app", "mine", "user_id", "温柔提醒")
            with patch("completion_monitor.get_tenant_token", return_value="token"), \
                 patch("completion_monitor.upload_image", return_value="img"), \
                 patch("completion_monitor.send_image_key") as image_sender, \
                 patch("completion_monitor.send_text_token",
                       side_effect=[None, RuntimeError("断网"), None]) as text_sender:
                with self.assertRaises(RuntimeError):
                    app.deliver_task(task_id, "secret")
                saved = app.outbox.load(task_id)
                self.assertTrue(saved["image_sent"])
                self.assertTrue(saved["time_sent"])
                self.assertFalse(saved["text_sent"])
                app.deliver_task(task_id, "secret")
            self.assertEqual(image_sender.call_count, 1)
            self.assertEqual([call.args[-2] for call in text_sender.call_args_list],
                             ["2026年9月16日 08:24:52", "温柔提醒", "温柔提醒"])
            self.assertEqual(text_sender.call_args_list[1].args[-1],
                             text_sender.call_args_list[2].args[-1])
            self.assertEqual(app.outbox.pending_ids(), [])

    def test_old_pending_task_gains_time_stage_before_unsent_reminder(self):
        app = object.__new__(App)
        app.events = queue.Queue()
        app.gif_enabled_value = False
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            shots = folder / "screenshots"
            shots.mkdir()
            screenshot = shots / "completion_20260916_082452_702754.png"
            screenshot.write_bytes(b"PNG")
            app.outbox = NotificationOutbox(folder / "pending", shots)
            task_id = app.outbox.create(screenshot, "app", "mine", "user_id", "温柔提醒")
            old_task = app.outbox.load(task_id)
            for field in ("capture_time", "time_uuid", "time_sent"):
                old_task.pop(field)
            old_task["image_key"] = "img"
            old_task["image_sent"] = True
            app.outbox.save(old_task)
            messages = []
            with patch("completion_monitor.get_tenant_token", return_value="token"), \
                 patch("completion_monitor.send_image_key") as image_sender, \
                 patch("completion_monitor.send_text_token",
                       side_effect=lambda _token, _recipient, _type, msg, _uuid: messages.append(msg)):
                app.deliver_task(task_id, "secret")
            image_sender.assert_not_called()
            self.assertEqual(messages, ["2026年9月16日 08:24:52", "温柔提醒"])

    def test_saved_screenshot_name_and_feishu_time_use_same_capture_time(self):
        captured_at = datetime(2026, 9, 16, 8, 24, 52, 702754)
        self.assertEqual(capture_time_text(captured_at), "2026年9月16日 08:24:52")
        self.assertEqual(capture_time_from_filename("completion_20260916_082452_702754.png"),
                         "2026年9月16日 08:24:52")
        capture = CapturedWindow(2, 1, b"\0" * 8, 0, 0)
        with tempfile.TemporaryDirectory() as temporary, \
             patch("completion_monitor.SCREENSHOT_DIR", Path(temporary)):
            from completion_monitor import save_screenshot
            screenshot = save_screenshot(capture, captured_at)
            self.assertEqual(screenshot.name, "completion_20260916_082452_702754.png")

    def test_failed_delivery_is_scheduled_again_without_blocking_queue(self):
        app = object.__new__(App)
        app.deliveries = queue.Queue()
        app.events = queue.Queue()
        app.shutdown_event = threading.Event()
        app.delivery_attempts = {}
        app.deliveries.put(("task", "secret"))
        calls = []

        def deliver(task_id, secret):
            calls.append((task_id, secret))
            if len(calls) == 1:
                raise RuntimeError("网络断开")
            app.shutdown_event.set()

        def timer(_delay, callback, args):
            return SimpleNamespace(daemon=True, start=lambda: callback(*args))

        with patch.object(app, "deliver_task", side_effect=deliver), \
             patch("completion_monitor.threading.Timer", side_effect=timer):
            app.delivery_loop()
        self.assertEqual(calls, [("task", "secret"), ("task", "secret")])
        self.assertEqual(app.deliveries.unfinished_tasks, 0)
        self.assertEqual(app.events.get_nowait()[0], "send_retry")

    def test_watchdog_turns_yellow_once_then_green_after_capture_recovers(self):
        app = object.__new__(App)
        app.monitor_health = MonitorHealth(0)
        app.stop_event = threading.Event()
        app.worker = SimpleNamespace(is_alive=lambda: True)
        app.monitor_interval = 1
        app.monitor_credentials = ("app", "secret", "mine", "user_id")
        app.monitor_light_state = "green"
        lights = []
        alerts = []
        app.set_monitor_light = lambda state: (lights.append(state), setattr(app, "monitor_light_state", state))
        app.queue_anomaly = lambda *args: alerts.append(args)
        app.log = lambda _message: None
        app.root = SimpleNamespace(after=lambda *_args: None, bell=lambda: None)
        with patch("completion_monitor.time.monotonic", return_value=50):
            app.check_monitor_health()
            app.check_monitor_health()
            app.monitor_health.last_progress = 50
            app.check_monitor_health()
        self.assertEqual(lights, ["yellow", "green"])
        self.assertEqual(len(alerts), 1)
        self.assertGreater(watchdog_timeout(1), 1)

    def test_monitor_unknown_picture_queues_one_alert_after_30_seconds(self):
        app = object.__new__(App)
        app.events = queue.Queue()
        app.deliveries = queue.Queue()
        stop_event = threading.Event()
        capture = CapturedWindow(2, 1, b"\0" * 8, 0, 0)
        remaining = [0, 15, 30, 31]

        def clock():
            now = remaining.pop(0)
            if not remaining:
                stop_event.set()
            return now

        with tempfile.TemporaryDirectory() as temporary, \
             patch("completion_monitor.capture_window", return_value=capture), \
             patch("completion_monitor.classify_status", return_value=("unknown", 0.0)), \
             patch("completion_monitor.time.monotonic", side_effect=clock):
            app.outbox = NotificationOutbox(Path(temporary) / "pending",
                                            Path(temporary) / "screenshots")
            app.monitor_loop(1, (0, 0, 1, 1), 0, "app", "secret", "recipient", "user_id", stop_event)
            self.assertEqual(len(app.outbox.pending_ids()), 1)
        self.assertEqual(app.deliveries.qsize(), 1)

    def test_cleanup_only_removes_program_screenshots_and_waits_for_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            screenshot = folder / "completion_123.png"
            other = folder / "my_photo.png"
            screenshot.write_bytes(b"PNG")
            other.write_bytes(b"PNG")
            self.assertEqual(stored_screenshots(folder), [screenshot])

            app = object.__new__(App)
            app.worker = None
            app.deliveries = queue.Queue()
            app.background_screenshots = 0
            app.log = lambda _message: None
            app.deliveries.put("sending")
            with patch("completion_monitor.SCREENSHOT_DIR", folder), \
                 patch("completion_monitor.messagebox.showinfo") as info, \
                 patch("completion_monitor.messagebox.askyesno") as confirm:
                app.cleanup_screenshots()
                info.assert_called_once()
                confirm.assert_not_called()
            self.assertTrue(screenshot.exists())

            app.deliveries.get_nowait()
            app.deliveries.task_done()
            def start_new_delivery(*_args):
                app.deliveries.put("sending again")
                return True

            with patch("completion_monitor.SCREENSHOT_DIR", folder), \
                 patch("completion_monitor.messagebox.askyesno", side_effect=start_new_delivery), \
                 patch("completion_monitor.messagebox.showinfo") as info:
                app.cleanup_screenshots()
                info.assert_called_once()
            self.assertTrue(screenshot.exists())
            app.deliveries.get_nowait()
            app.deliveries.task_done()
            with patch("completion_monitor.SCREENSHOT_DIR", folder), \
                 patch("completion_monitor.messagebox.askyesno", return_value=True):
                app.cleanup_screenshots()
            self.assertFalse(screenshot.exists())
            self.assertTrue(other.exists())

    def test_green_and_purple_sampled_regions(self):
        green = bytes((0, 255, 0, 255)) * 200 * 60
        purple = bytes((132, 0, 132, 255)) * 200 * 60
        region = (10, 10, 190, 50)
        self.assertEqual(classify_status(green, 200, 60, region)[0], "testing")
        self.assertEqual(classify_status(purple, 200, 60, region)[0], "credence")

    def test_png_preserves_colors(self):
        png = CapturedWindow(2, 1, bytes((0, 255, 0, 0, 132, 0, 132, 0)), 0, 0).png()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        position = 8
        while position < len(png):
            length = struct.unpack(">I", png[position:position + 4])[0]
            tag = png[position + 4:position + 8]
            data = png[position + 8:position + 8 + length]
            if tag == b"IDAT":
                self.assertEqual(zlib.decompress(data),
                                 bytes((0, 0, 255, 0, 255, 132, 0, 132, 255)))
                break
            position += length + 12
        else:
            self.fail("PNG 缺少图像数据")

    def test_feishu_sends_image_and_exact_text(self):
        sent = []

        def fake_json(url, payload, token=""):
            sent.append(payload)
            return {"code": 0}

        with patch.object(feishu_client, "get_tenant_token", return_value="token"), \
             patch.object(feishu_client, "_request", return_value={"code": 0, "data": {"image_key": "img_test"}}), \
             patch.object(feishu_client, "_json_request", side_effect=fake_json):
            feishu_client.send_completion("app", "secret", "oc_test", b"PNG")
        self.assertEqual([item["msg_type"] for item in sent], ["image", "text"])
        self.assertEqual(json.loads(sent[0]["content"]), {"image_key": "img_test"})
        self.assertEqual(json.loads(sent[1]["content"]), {"text": "测试已结束"})

    def test_private_email_uses_email_recipient(self):
        sent = []

        def fake_json(url, payload, token=""):
            sent.append((url, payload))
            return {"code": 0}

        with patch.object(feishu_client, "get_tenant_token", return_value="token"), \
             patch.object(feishu_client, "_request", return_value={"code": 0, "data": {"image_key": "img_test"}}), \
             patch.object(feishu_client, "_json_request", side_effect=fake_json):
            feishu_client.send_completion("app", "secret", "me@example.com", b"PNG",
                                          recipient_type="email")
        self.assertTrue(all("receive_id_type=email" in url for url, _ in sent))
        self.assertTrue(all(payload["receive_id"] == "me@example.com" for _, payload in sent))

    def test_feishu_anomaly_sends_text_only_to_configured_user(self):
        sent = []
        with patch.object(feishu_client, "get_tenant_token", return_value="token"), \
             patch.object(feishu_client, "_json_request", side_effect=lambda url, payload, token="": sent.append((url, payload))):
            feishu_client.send_text("app", "secret", "my_user", "监控异常", "user_id")
        self.assertEqual(len(sent), 1)
        self.assertIn("receive_id_type=user_id", sent[0][0])
        self.assertEqual(sent[0][1]["msg_type"], "text")
        self.assertEqual(json.loads(sent[0][1]["content"]), {"text": "监控异常"})

    def test_private_screenshot_command_checks_exact_sender_and_message(self):
        def message(text="截图", user_id="mine", open_id="ou_mine",
                    chat_type="p2p", sender_type="user", content_type="text"):
            return SimpleNamespace(chat_id="oc_private", chat_type=chat_type,
                                   raw_content_type=content_type, content_text=text,
                                   sender_type=sender_type,
                                   sender=SimpleNamespace(user_id=user_id, open_id=open_id))

        self.assertEqual(screenshot_request(message(), "mine"), "authorized")
        self.assertEqual(screenshot_request(message(text="jt"), "mine"), "authorized")
        self.assertEqual(screenshot_request(message(text="JT"), "mine"), "authorized")
        self.assertEqual(screenshot_request(message(), "ou_mine"), "authorized")
        self.assertEqual(screenshot_request(message(user_id="coworker"), "mine"), "unauthorized")
        self.assertEqual(screenshot_request(message(user_id=None), "mine"), "unauthorized")
        self.assertIsNone(screenshot_request(message(chat_type="group"), "mine"))
        self.assertIsNone(screenshot_request(message(sender_type="bot"), "mine"))
        self.assertIsNone(screenshot_request(message(text="截图 现在"), "mine"))
        self.assertIsNone(screenshot_request(message(text="/截图mstsc"), "mine"))
        self.assertIsNone(screenshot_request(message(content_type="post"), "mine"))

    def test_replayed_command_is_not_processed_twice(self):
        seen = SeenCommands(limit=2)
        self.assertTrue(seen.add("om_1"))
        self.assertFalse(seen.add("om_1"))
        self.assertTrue(seen.add("om_2"))
        self.assertTrue(seen.add("om_3"))
        self.assertTrue(seen.add("om_1"))

    def test_coworker_command_replies_unauthorized_without_capture(self):
        requests = []
        reports = []
        sent = []
        listener = FeishuCommandListener("app", "secret", "mine", requests.append, reports.append)
        coworker = SimpleNamespace(
            chat_id="oc_coworker", chat_type="p2p", raw_content_type="text",
            content_text="jt", sender_type="user", message_id="om_command",
            sender=SimpleNamespace(user_id="coworker", open_id="ou_coworker"))

        class FakeChannel:
            def __init__(self, **_kwargs):
                self.handlers = {}

            def on(self, event, handler):
                self.handlers[event] = handler

            async def connect_until_ready(self, **_kwargs):
                self.handlers["message"](coworker)
                self.handlers["message"](coworker)
                await asyncio.sleep(0.02)
                listener.stop()

            async def disconnect(self):
                pass

        with patch("feishu_command_listener.send_text", side_effect=lambda *args: sent.append(args)):
            asyncio.run(listener._session(FakeChannel))
        self.assertEqual(requests, [])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2:], ("oc_coworker", "此指令未授权", "chat_id"))

    def test_authorized_command_sends_current_window_to_request_chat(self):
        app = object.__new__(App)
        app.command_enabled = SimpleNamespace(get=lambda: True)
        listener = object()
        app.command_listener = listener
        app.selected_hwnd = 17
        app.background_screenshots = 0
        app.events = queue.Queue()
        app.log = lambda _message: None
        capture = CapturedWindow(2, 1, b"\0" * 8, 0, 0)
        with tempfile.TemporaryDirectory() as temporary, \
             patch("completion_monitor.capture_window", return_value=capture), \
             patch("completion_monitor.save_screenshot", return_value=Path(temporary) / "shot.png"), \
             patch("completion_monitor.send_completion") as sender:
            (Path(temporary) / "shot.png").write_bytes(b"PNG")
            app.capture_for_command(listener, "oc_requester", "app", "secret")
            while app.background_screenshots and app.events.qsize() < 2:
                threading.Event().wait(0.01)
            events = [app.events.get_nowait()[0] for _ in range(2)]
        self.assertEqual(events, ["command_sent", "screenshot_task_finished"])
        self.assertEqual(sender.call_args.args[:3], ("app", "secret", "oc_requester"))
        self.assertEqual(sender.call_args.args[-1], "chat_id")

    def test_gentle_messages_rotate_without_claiming_pass(self):
        self.assertGreaterEqual(len(MESSAGES), 5)
        self.assertEqual(len(set(MESSAGES)), len(MESSAGES))
        self.assertTrue(all("测试已结束" in message or "测试结束" in message for message in MESSAGES))
        self.assertFalse(any("通过" in message or "成功" in message for message in MESSAGES))
        self.assertEqual(message_for(len(MESSAGES)), MESSAGES[0])

    def test_custom_completion_message_is_sent(self):
        sent = []

        def fake_json(url, payload, token=""):
            sent.append(payload)
            return {"code": 0}

        with patch.object(feishu_client, "get_tenant_token", return_value="token"), \
             patch.object(feishu_client, "_request", return_value={"code": 0, "data": {"image_key": "img_test"}}), \
             patch.object(feishu_client, "_json_request", side_effect=fake_json):
            feishu_client.send_completion("app", "secret", "oc_test", b"PNG", MESSAGES[0])
        self.assertEqual(json.loads(sent[1]["content"]), {"text": MESSAGES[0]})

    def test_chooses_a_valid_gif_from_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            good = folder / "reaction.gif"
            good.write_bytes(b"GIF89a" + struct.pack("<HH", 300, 300) + b"body")
            (folder / "broken.gif").write_bytes(b"not a gif")
            (folder / "too_large.gif").write_bytes(b"GIF89a" + struct.pack("<HH", 2001, 1) + b"body")
            self.assertEqual(valid_gifs(folder), [good])
            self.assertEqual(choose_gif(folder), good)

    def test_gif_is_sent_after_screenshot_and_text(self):
        sent = []
        uploads = []

        def fake_json(url, payload, token=""):
            sent.append(payload)
            return {"code": 0}

        def fake_upload(url, body, content_type, token=""):
            uploads.append(body)
            return {"code": 0, "data": {"image_key": f"img_{len(uploads)}"}}

        with patch.object(feishu_client, "get_tenant_token", return_value="token"), \
             patch.object(feishu_client, "_request", side_effect=fake_upload), \
             patch.object(feishu_client, "_json_request", side_effect=fake_json):
            token = feishu_client.send_completion("app", "secret", "oc_test", b"PNG")
            feishu_client.send_gif(token, "oc_test", b"GIF89a" + b"body")
        self.assertEqual([item["msg_type"] for item in sent], ["image", "text", "image"])
        self.assertIn(b"Content-Type: image/gif", uploads[1])
        self.assertIn(b"reaction.gif", uploads[1])


if __name__ == "__main__":
    unittest.main()
