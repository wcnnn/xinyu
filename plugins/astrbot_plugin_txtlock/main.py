"""文本加密锁：给桌面（或任意路径）的文本文件加密码。

用法（仅管理员）：
  /加密 我的AstrBot.txt 密码
  /解密 我的AstrBot.txt 密码

加密后就地变成密文（文件开头有标记），只有正确密码能还原。
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

MARK = b"LOCKED:v1:"
ITER = 200_000
DESKTOP = Path(os.path.expanduser("~")) / "Desktop"


def _key(password: str, salt: bytes) -> bytes:
    raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITER, dklen=32)
    return base64.urlsafe_b64encode(raw)


def _resolve(name: str) -> Path:
    p = Path(name)
    if p.is_absolute():
        return p
    candidate = DESKTOP / name
    if candidate.exists():
        return candidate
    return p


@register(
    "astrbot_plugin_txtlock",
    "local",
    "文本加密锁：给文本文件加密码",
    "v1.0.0",
)
class TxtLockPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @staticmethod
    def _is_locked(data: bytes) -> bool:
        return data.startswith(MARK)

    def _encrypt(self, path: Path, password: str) -> None:
        from cryptography.fernet import Fernet

        data = path.read_bytes()
        if self._is_locked(data):
            raise ValueError("这个文件已经加密过了。")
        salt = os.urandom(16)
        token = Fernet(_key(password, salt)).encrypt(data)
        payload = MARK + base64.b64encode(salt) + b":" + token
        path.write_bytes(payload)

    def _decrypt(self, path: Path, password: str) -> None:
        from cryptography.fernet import Fernet, InvalidToken

        data = path.read_bytes()
        if not self._is_locked(data):
            raise ValueError("这个文件没加密。")
        rest = data[len(MARK):]
        salt_b64, _, token = rest.partition(b":")
        salt = base64.b64decode(salt_b64)
        try:
            plain = Fernet(_key(password, salt)).decrypt(token)
        except InvalidToken as exc:
            raise ValueError("密码不对。") from exc
        path.write_bytes(plain)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("加密")
    async def lock_cmd(self, event: AstrMessageEvent):
        """加密：/加密 <文件名或路径> <密码>"""
        parts = (getattr(event, "message_str", "") or "").strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法：/加密 <文件名> <密码>")
            return
        path = _resolve(parts[1])
        if not path.exists():
            yield event.plain_result(f"找不到文件：{path}")
            return
        try:
            self._encrypt(path, parts[2])
            yield event.plain_result(f"已加密：{path.name}（密码只有你知道，忘了没法恢复）")
        except Exception as exc:
            logger.warning(f"[文本锁] 加密失败：{exc}")
            yield event.plain_result(f"加密失败：{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("解密")
    async def unlock_cmd(self, event: AstrMessageEvent):
        """解密：/解密 <文件名或路径> <密码>"""
        parts = (getattr(event, "message_str", "") or "").strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法：/解密 <文件名> <密码>")
            return
        path = _resolve(parts[1])
        if not path.exists():
            yield event.plain_result(f"找不到文件：{path}")
            return
        try:
            self._decrypt(path, parts[2])
            yield event.plain_result(f"已解密：{path.name}")
        except Exception as exc:
            logger.warning(f"[文本锁] 解密失败：{exc}")
            yield event.plain_result(f"解密失败：{exc}")
