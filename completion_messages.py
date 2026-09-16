"""Gentle but neutral completion reminders; no pass/fail claim."""


MESSAGES = (
    "这轮测试已结束啦。我把结束时的画面截好并发给你，方便你随时回看。",
    "测试已结束，我刚刚确认状态已经切换。结束画面也一并送上，辛苦啦。",
    "这轮测试已结束。我已为你保存当时的截图，需要查看时打开这条消息就好。",
    "测试已结束啦。我一直帮你留意着状态，结束时的画面现在已经发到你手边。",
    "已确认测试结束。我把结束画面一起送来，方便你从容查看这一轮的结果。",
    "测试已结束，轻轻提醒你一声：当时的截图已经保存并发送，可以随时查看。",
)


def message_for(completion_number: int) -> str:
    return MESSAGES[completion_number % len(MESSAGES)]
