"""按需翻阅：让她自己决定什么时候去翻学习笔记和日记。

与「全局注入」的区别：这里不花每轮上下文，只在她主动调用工具或发命令时才检索。
"""

from __future__ import annotations

import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_learning_recall",
    "local",
    "学习笔记检索：按需翻看她自己记下的东西",
    "v1.0.0",
)
class LearningRecallPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @property
    def _cfg(self) -> dict:
        return getattr(self, "config", None) or {}

    @staticmethod
    def _is_group(event) -> bool:
        try:
            message_type = event.get_message_type()
        except Exception:
            return False
        return "GROUP" in (getattr(message_type, "name", "") or str(message_type)).upper()

    def _kb_names(self) -> list[str]:
        raw = str(self._cfg.get("kb_names") or "糯糯的学习笔记")
        return [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]

    async def _search(self, query: str) -> str:
        names = self._kb_names()
        try:
            result = await self.context.kb_manager.retrieve(
                query=query,
                kb_names=names,
                top_k_fusion=20,
                top_m_final=int(self._cfg.get("top_k", 4) or 4),
            )
        except Exception as exc:
            logger.warning(f"[学习笔记] 检索失败：{exc}")
            return f"检索出错：{exc}"
        if not result or not (result.get("results") or []):
            return "没翻到相关的东西。"
        text = result.get("context_text") or ""
        limit = int(self._cfg.get("max_chars", 1200) or 1200)
        return text[:limit] if text else "没翻到相关的东西。"

    async def _write_note(self, topic: str, content: str) -> bool:
        kb = await self.context.kb_manager.get_kb_by_name("糯糯的学习笔记")
        if kb is None:
            logger.warning("[学习笔记] 知识库「糯糯的学习笔记」不存在")
            return False
        stamp = time.strftime("%Y%m%d-%H%M%S")
        body = (
            f"# 学习笔记：{topic}\n\n"
            f"- 时间：{time.strftime('%Y-%m-%d %H:%M')}\n"
            f"- 来源：日常聊天\n\n"
            f"{content}\n"
        )
        await kb.upload_document(
            file_name=f"learn-chat-{stamp}.md",
            file_content=body.encode("utf-8"),
            file_type="md",
        )
        return True

    @filter.llm_tool(name="remember_learning")
    async def remember_learning(self, event: AstrMessageEvent, topic: str, content: str):
        """聊天里学到、值得长期记住的东西，记进自己的学习笔记。

        只有当你（糯糯）觉得这条内容以后还用得上时才调用，闲聊和情绪不要记。

        Args:
            topic(string): 一句话主题，例如"怎么挑西瓜""为什么天空是蓝的"
            content(string): 记下来的内容，一两句，不超过 120 字
        """
        if self._is_group(event):
            yield "群里不记笔记。"
            return
        topic = str(topic or "").strip()[:60]
        content = str(content or "").strip()[:200]
        if not topic or not content:
            yield "主题或内容为空，没有记录。"
            return
        ok = await self._write_note(topic, content)
        yield "记好了。" if ok else "知识库不在，没记成。"

    @filter.llm_tool(name="recall_learning")
    async def recall_learning(self, event: AstrMessageEvent, query: str):
        """翻阅自己以前记下的学习笔记和日记。当需要回忆"我以前刷到过/记过什么"时调用。

        Args:
            query(string): 想找的主题或关键词，例如"关于猫的知识""上次刷到的做饭技巧"
        """
        text = await self._search(str(query or "").strip())
        yield text

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("笔记")
    async def notes_command(self, event: AstrMessageEvent):
        """笔记：/笔记 查 <关键词>"""
        parts = (getattr(event, "message_str", "") or "").strip().split()
        if len(parts) < 3 or parts[1] not in ("查", "搜", "找"):
            yield event.plain_result("用法：/笔记 查 <关键词>")
            return
        yield event.plain_result(await self._search(" ".join(parts[2:])))
