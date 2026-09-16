"""Encrypt app secrets for the current Windows user using DPAPI."""

import base64
import ctypes
from ctypes import wintypes


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
                                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                   wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                     wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def _blob(data: bytes):
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return DATA_BLOB(len(data), buffer), buffer


def protect(secret: str) -> str:
    source, keepalive = _blob(secret.encode("utf-8"))
    target = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(source), "MSTSC Feishu secret",
                                   None, None, None, 0, ctypes.byref(target)):
        raise OSError(ctypes.get_last_error(), "无法加密飞书凭证")
    try:
        return base64.b64encode(ctypes.string_at(target.pbData, target.cbData)).decode("ascii")
    finally:
        kernel32.LocalFree(target.pbData)


def unprotect(ciphertext: str) -> str:
    source, keepalive = _blob(base64.b64decode(ciphertext))
    target = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None,
                                     0, ctypes.byref(target)):
        raise OSError(ctypes.get_last_error(), "无法解密飞书凭证")
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(target.pbData)
