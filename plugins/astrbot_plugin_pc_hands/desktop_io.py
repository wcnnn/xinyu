"""Windows 桌面输入/截图的底层封装（不依赖 pyautogui）。

- 截图：PIL ImageGrab
- 鼠标：SetCursorPos + mouse_event
- 键盘：SendInput 的 Unicode 模式
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


VK = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
}


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def screenshot(path: str) -> tuple[int, int]:
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=True)
    img.save(path, "JPEG", quality=75)
    return img.size


def move(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def _mouse(flags: int) -> None:
    user32.mouse_event(flags, 0, 0, 0, 0)


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    move(x, y)
    time.sleep(0.08)
    if button == "right":
        _mouse(MOUSEEVENTF_RIGHTDOWN)
        time.sleep(0.03)
        _mouse(MOUSEEVENTF_RIGHTUP)
        return
    _mouse(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.03)
    _mouse(MOUSEEVENTF_LEFTUP)
    if double:
        time.sleep(0.08)
        _mouse(MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.03)
        _mouse(MOUSEEVENTF_LEFTUP)


def _send_unicode(text: str) -> None:
    for ch in text:
        code = ord(ch)
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.012)


def type_text(text: str) -> None:
    _send_unicode(str(text))


def press(key: str, times: int = 1) -> None:
    name = str(key).strip().lower()
    if name in VK:
        vk = VK[name]
    elif len(name) == 1:
        _send_unicode(name)
        return
    else:
        raise ValueError(f"不认识的键：{key}")
    for _ in range(max(1, times)):
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)


def hotkey(*keys: str) -> None:
    codes = []
    for key in keys:
        name = str(key).strip().lower()
        if name in VK:
            codes.append(VK[name])
        elif len(name) == 1 and (name.isalpha() or name.isdigit()):
            codes.append(ord(name.upper()))
        else:
            raise ValueError(f"暂不支持这个键：{key}")
    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
        time.sleep(0.02)
    for code in reversed(codes):
        user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)
