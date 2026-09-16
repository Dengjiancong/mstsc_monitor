"""Capture an mstsc window, including when another window covers it."""

from __future__ import annotations

import ctypes
import os
import struct
import zlib
from dataclasses import dataclass
from ctypes import wintypes


if os.name != "nt":
    raise SystemExit("窗口捕获只支持 Windows。")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    user32.SetProcessDPIAware()
except OSError:
    pass

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
BI_RGB = 0
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                  ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def _process_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def list_mstsc_windows() -> list[tuple[int, str]]:
    windows = []
    names = {}
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in names:
            names[pid.value] = _process_name(pid.value)
        if names[pid.value] != "mstsc.exe":
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        if rect.right - rect.left < 400 or rect.bottom - rect.top < 250:
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, len(title))
        windows.append((int(hwnd), title.value or f"mstsc 窗口 {int(hwnd)}"))
        return True

    callback_ref = callback_type(callback)
    user32.EnumWindows(callback_ref, 0)
    return windows


@dataclass(frozen=True)
class CapturedWindow:
    width: int
    height: int
    bgra: bytes
    left: int
    top: int

    def png(self) -> bytes:
        rows = []
        stride = self.width * 4
        for y in range(self.height):
            row = bytearray(self.bgra[y * stride:(y + 1) * stride])
            blue = row[0::4]
            row[0::4] = row[2::4]
            row[2::4] = blue
            row[3::4] = b"\xff" * self.width
            rows.append(b"\x00" + row)
        raw = zlib.compress(b"".join(rows), 6)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def capture_window(hwnd: int) -> CapturedWindow:
    if not user32.IsWindow(hwnd):
        raise RuntimeError("mstsc 窗口已经关闭；请重新选择窗口")
    if user32.IsIconic(hwnd):
        raise RuntimeError("mstsc 已最小化；此模式只支持窗口被遮挡")
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("无法获取 mstsc 窗口位置")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0 or width * height > 20_000_000:
        raise RuntimeError("mstsc 窗口尺寸无效")
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    dc = gdi32.CreateCompatibleDC(None)
    if not dc:
        raise RuntimeError("无法建立窗口截图缓冲区")
    bitmap = None
    old = None
    try:
        bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise RuntimeError("无法分配窗口截图缓冲区")
        old = gdi32.SelectObject(dc, bitmap)
        # PrintWindow asks mstsc itself to draw, instead of reading the covered desktop pixels.
        if not user32.PrintWindow(hwnd, dc, 0):
            raise RuntimeError("mstsc 未能提供窗口画面；请用“测试截图”检查连接状态")
        bgra = ctypes.string_at(bits, width * height * 4)
        return CapturedWindow(width, height, bgra, rect.left, rect.top)
    finally:
        if old:
            gdi32.SelectObject(dc, old)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
