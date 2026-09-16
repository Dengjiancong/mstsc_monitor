"""Send a local screenshot and completion text using a Feishu app bot."""

from __future__ import annotations

import json
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE = "https://open.feishu.cn/open-apis"


def _request(url: str, body: bytes, content_type: str, token: str = "") -> dict:
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read(500).decode("utf-8", "replace")
        raise RuntimeError(f"飞书 HTTP {error.code}: {detail}") from error
    except (URLError, OSError) as error:
        raise RuntimeError(f"无法连接飞书：{error}") from error
    if result.get("code") != 0:
        raise RuntimeError(f"飞书返回 {result.get('code')}: {result.get('msg')}")
    return result


def _json_request(url: str, payload: dict, token: str = "") -> dict:
    return _request(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8", token)


def _multipart_image(data: bytes, filename: str = "completion.png",
                     mime_type: str = "image/png") -> tuple[bytes, str]:
    boundary = "----mstsc" + secrets.token_hex(12)
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image_type\"\r\n\r\nmessage\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("ascii") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


def get_tenant_token(app_id: str, app_secret: str) -> str:
    token_result = _json_request(BASE + "/auth/v3/tenant_access_token/internal",
                                 {"app_id": app_id, "app_secret": app_secret})
    token = token_result.get("tenant_access_token")
    if not token:
        raise RuntimeError("飞书未返回应用访问令牌")
    return token


def list_bot_chats(app_id: str, app_secret: str) -> list[tuple[str, str]]:
    token = get_tenant_token(app_id, app_secret)
    chats = []
    page_token = ""
    while True:
        query = {"page_size": "100"}
        if page_token:
            query["page_token"] = page_token
        request = Request(BASE + "/im/v1/chats?" + urlencode(query),
                          headers={"Authorization": "Bearer " + token}, method="GET")
        try:
            with urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError) as error:
            raise RuntimeError(f"无法获取飞书群聊：{error}") from error
        if result.get("code") != 0:
            raise RuntimeError(f"飞书返回 {result.get('code')}: {result.get('msg')}")
        data = result.get("data", {})
        chats.extend((item["chat_id"], item.get("name") or item["chat_id"])
                     for item in data.get("items", []) if item.get("chat_id"))
        if not data.get("has_more"):
            return chats
        page_token = data.get("page_token", "")
        if not page_token:
            return chats


def send_completion(app_id: str, app_secret: str, recipient_id: str, png: bytes,
                    message: str = "测试已结束", recipient_type: str = "chat_id") -> str:
    token = get_tenant_token(app_id, app_secret)
    image_key = upload_image(token, png)
    send_image_key(token, recipient_id, recipient_type, image_key)
    send_text_token(token, recipient_id, recipient_type, message)
    return token


def upload_image(token: str, png: bytes) -> str:
    body, content_type = _multipart_image(png)
    image_result = _request(BASE + "/im/v1/images", body, content_type, token)
    image_key = image_result.get("data", {}).get("image_key")
    if not image_key:
        raise RuntimeError("飞书未返回图片标识")
    return image_key


def send_image_key(token: str, recipient_id: str, recipient_type: str,
                   image_key: str, message_uuid: str = "") -> None:
    if recipient_type not in {"chat_id", "open_id", "user_id", "email"}:
        raise ValueError("不支持的飞书接收地址类型")
    url = BASE + "/im/v1/messages?" + urlencode({"receive_id_type": recipient_type})
    payload = {"receive_id": recipient_id, "msg_type": "image",
               "content": json.dumps({"image_key": image_key})}
    if message_uuid:
        payload["uuid"] = message_uuid
    _json_request(url, payload, token)


def send_text_token(token: str, recipient_id: str, recipient_type: str,
                    message: str, message_uuid: str = "") -> None:
    if recipient_type not in {"chat_id", "open_id", "user_id", "email"}:
        raise ValueError("不支持的飞书接收地址类型")
    url = BASE + "/im/v1/messages?" + urlencode({"receive_id_type": recipient_type})
    payload = {"receive_id": recipient_id, "msg_type": "text",
               "content": json.dumps({"text": message}, ensure_ascii=False)}
    if message_uuid:
        payload["uuid"] = message_uuid
    _json_request(url, payload, token)


def send_text(app_id: str, app_secret: str, recipient_id: str, message: str,
              recipient_type: str = "chat_id") -> None:
    token = get_tenant_token(app_id, app_secret)
    send_text_token(token, recipient_id, recipient_type, message)


def send_gif(token: str, recipient_id: str, gif: bytes,
             recipient_type: str = "chat_id") -> None:
    if recipient_type not in {"chat_id", "open_id", "user_id", "email"}:
        raise ValueError("不支持的飞书接收地址类型")
    if not gif.startswith((b"GIF87a", b"GIF89a")) or len(gif) > 10_000_000:
        raise ValueError("表情包不是有效的 GIF，或文件超过 10 MB")
    body, content_type = _multipart_image(gif, "reaction.gif", "image/gif")
    image_result = _request(BASE + "/im/v1/images", body, content_type, token)
    image_key = image_result.get("data", {}).get("image_key")
    if not image_key:
        raise RuntimeError("飞书未返回 GIF 图片标识")
    url = BASE + "/im/v1/messages?" + urlencode({"receive_id_type": recipient_type})
    _json_request(url, {"receive_id": recipient_id, "msg_type": "image",
                        "content": json.dumps({"image_key": image_key})}, token)
