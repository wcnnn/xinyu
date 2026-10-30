"""糯糯的表情收藏：看到喜欢的表情包可以自己存下来，之后能被 meme_manager 检索使用。

安全设计（四道过滤）：
1. 域名白名单 —— 只接受常见表情站/图床
2. 内容类型 —— 必须是 image/*
3. 真实格式 —— 用 PIL 校验，不认伪装的二进制
4. 描述审核 —— 标签和说明不能含色情/暴力等词
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import shutil
import urllib.parse
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

ALLOWED_HOSTS = (
    "i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com",
    "p3-sign.douyinpic.com", "p6-sign.douyinpic.com", "p9-sign.douyinpic.com",
    "p26-sign.douyinpic.com",
    "gchat.qpic.cn", "multimedia.nt.qq.com.cn", "q1.qlogo.cn",
    "cdn.jsdelivr.net", "raw.githubusercontent.com",
    "s3.bmp.ovh", "img.picui.cn", "i.imgur.com", "media.giphy.com",
)

BLOCKED_WORDS = (
    "色情", "裸", "露点", "做爱", "约炮", "黄图", "涩图",
    "暴力", "血腥", "自杀", "毒品", "赌博", "反动",
)

MAX_BYTES = 4 * 1024 * 1024

# 情绪目录（对齐 meme_manager 的分类）
EMOTIONS = ("affection", "angry", "confused", "entertainment", "food",
            "greeting", "happy", "money", "reply", "sad", "shy", "sleep")

PACK_NAME = "nuonuo-favorites"


@register(
    "astrbot_plugin_nuonuo_meme_save",
    "local",
    "糯糯的表情收藏：可以自己保存喜欢的表情包",
    "v1.0.0",
)
class MemeSavePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_nuonuo_meme_save")
        self.log_path = self.data_dir / "saved.log"

    # ------------------------------------------------------------------ 路径

    def _packs_root(self) -> Path:
        cfg = getattr(self, "config", None) or {}
        explicit = str(cfg.get("meme_packs_root") or "").strip()
        if explicit:
            return Path(explicit)
        return Path(__file__).resolve().parents[2] / "plugin_data" / "meme_manager" / "packs"

    def _pack_dir(self) -> Path:
        return self._packs_root() / PACK_NAME

    def _ensure_pack(self) -> Path:
        pack = self._pack_dir()
        (pack / "memes").mkdir(parents=True, exist_ok=True)
        manifest = pack / "manifest.json"
        if not manifest.exists():
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "id": PACK_NAME,
                "name": "糯糯存的表情包",
                "version": "1.0.0",
                "description": "糯糯自己看到喜欢就存下来的表情包。",
                "author": "糯糯",
                "license": "各图版权归原作者，仅本地个人使用",
                "tags": ["personal", "nuonuo"],
            }, ensure_ascii=False, indent=2), "utf-8")
        return pack

    # ------------------------------------------------------------------ 审核

    def _check_url(self, url: str) -> str | None:
        if not url.lower().startswith(("http://", "https://")):
            return "链接格式不对，要 http/https 开头。"
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not host:
            return "链接里没有域名。"
        if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
            return f"这个来源不在允许范围里（{host}）。"
        return None

    def _check_words(self, text: str) -> str | None:
        low = (text or "").lower()
        for word in BLOCKED_WORDS:
            if word in low:
                return f"描述里有不合适的东西（{word}），这个不存。"
        return None

    # ------------------------------------------------------------------ 工具

    @filter.llm_tool(name="save_meme")
    async def save_meme(self, event: AstrMessageEvent, url: str, mood: str, tags: str, caption: str = ""):
        """把看到的好看表情包存进自己的图库，以后聊天可以拿出来用。

        Args:
            url(string): 表情包图片直链
            mood(string): 归到哪个情绪目录，可选 affection/angry/confused/entertainment/food/greeting/happy/money/reply/sad/shy/sleep
            tags(string): 标签，空格分隔，比如「无语 翻白眼 嫌弃」
            caption(string): 一句话说明它表达什么
        """
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            yield "现在不存表情包。"
            return

        url = str(url or "").strip()
        mood = str(mood or "").strip().lower()
        tags = str(tags or "").strip()
        caption = str(caption or "").strip()

        reason = self._check_url(url)
        if reason:
            self._log(f"拒绝(域名) {url[:70]} -> {reason}")
            yield reason
            return
        reason = self._check_words(tags + " " + caption)
        if reason:
            self._log(f"拒绝(内容) {tags[:40]} -> {reason}")
            yield reason
            return
        if mood not in EMOTIONS:
            yield f"情绪只能是这些之一：{', '.join(EMOTIONS)}"
            return

        # 下载
        try:
            import requests

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            with requests.get(url, headers=headers, timeout=25, stream=True) as resp:
                resp.raise_for_status()
                ctype = (resp.headers.get("content-type") or "").lower()
                if "image" not in ctype:
                    yield f"这个链接不是图片（{ctype or '未知'}）。"
                    return
                chunks, size = [], 0
                for chunk in resp.iter_content(65536):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_BYTES:
                        yield "图太大了（超过 4MB）。"
                        return
                    chunks.append(chunk)
            raw = b"".join(chunks)
        except Exception as exc:
            yield f"下载失败：{exc}"
            return

        # 真实格式校验
        try:
            from PIL import Image

            probe = Image.open(io.BytesIO(raw))
            probe.verify()
            fmt = (probe.format or "").lower()
            if fmt not in ("jpeg", "png", "gif", "webp", "bmp"):
                yield f"不支持的图片格式（{fmt}）。"
                return
        except Exception:
            yield "下载到的不是有效图片。"
            return

        # 落盘
        try:
            pack = self._ensure_pack()
            target_dir = pack / "memes" / mood
            target_dir.mkdir(parents=True, exist_ok=True)
            ext = "jpg" if fmt == "jpeg" else fmt
            name = hashlib.md5(raw).hexdigest()[:16] + "." + ext
            path = target_dir / name
            if path.exists():
                yield "这个表情包我已经存过了。"
                return
            path.write_bytes(raw)
            (target_dir / (name + ".txt")).write_text(
                f"tags：{tags}\n说明：{caption}\n", "utf-8"
            )
        except Exception as exc:
            yield f"保存失败：{exc}"
            return

        self._log(f"保存 {mood}/{name} tags={tags[:50]}")
        yield f"存好了，归到「{mood}」，标签：{tags}"

    @filter.llm_tool(name="list_saved_memes")
    async def list_saved_memes(self, event: AstrMessageEvent):
        """看看自己存了哪些表情包。"""
        pack = self._pack_dir()
        base = pack / "memes"
        if not base.exists():
            yield "还没存过表情包。"
            return
        lines, total = [], 0
        for mood_dir in sorted(base.iterdir()):
            if not mood_dir.is_dir():
                continue
            files = [f for f in mood_dir.iterdir()
                     if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")]
            if files:
                total += len(files)
                lines.append(f"- {mood_dir.name}: {len(files)} 个")
        if not total:
            yield "还没存过表情包。"
            return
        yield f"我一共存了 {total} 个：\n" + "\n".join(lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("表情库")
    async def show_lib(self, event: AstrMessageEvent):
        """表情库：看看她存了多少表情包。"""
        pack = self._pack_dir()
        base = pack / "memes"
        if not base.exists():
            yield event.plain_result("还没存过表情包。")
            return
        lines = [f"目录：{pack}"]
        for mood_dir in sorted(base.iterdir()):
            if mood_dir.is_dir():
                n = len([f for f in mood_dir.iterdir()
                         if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")])
                if n:
                    lines.append(f"  {mood_dir.name}: {n}")
        yield event.plain_result("\n".join(lines))

    def _log(self, text: str) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
        except Exception:
            pass