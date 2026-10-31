"""朋友档案：主人亲自登记的朋友名册。

- 必要字段（永不压缩）：QQ 号、称呼、关系
- 备注：可压缩，每 7 天把过期备注摘要成一句
- 添加方式：管理员命令 `/朋友 加 <QQ> <称呼> [关系]`，或对话中让模型调用 remember_friend
- 每轮对话把名单注入系统提示，保证她知道"谁是谁"
"""

from __future__ import annotations

import asyncio
import json
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

KB_NAME = "爸爸的亲朋"
KB_OWN = "糯糯的朋友"
WEEK_SECONDS = 7 * 24 * 60 * 60
COMPACT_CHECK_INTERVAL = 3600


@register(
    "astrbot_plugin_friend_book",
    "local",
    "朋友档案：QQ/称呼/关系名册，支持对话添加，每 7 天压缩过期备注",
    "v1.0.0",
)
class FriendBookPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_friend_book")
        self.store_path = self.data_dir / "friends.json"
        self.store = self._load()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ 存储

    def _load(self) -> dict:
        try:
            data = json.loads(self.store_path.read_text("utf-8"))
            data.setdefault("friends", {})
            data.setdefault("my_friends", {})
            data.setdefault("meta", {"last_compact": 0})
            return data
        except Exception:
            return {"friends": {}, "my_friends": {}, "meta": {"last_compact": 0}}

    def _save(self) -> None:
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.store, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.store_path)

    # ------------------------------------------------------------------ 知识库

    async def _kb(self):
        try:
            return await self.context.kb_manager.get_kb_by_name(KB_NAME)
        except Exception as exc:
            logger.warning(f"[朋友档案] 知识库不可用：{exc}")
            return None

    @staticmethod
    def _doc_text(rec: dict) -> str:
        lines = [
            f"# 爸爸的亲朋：{rec.get('name', '')}",
            f"- QQ：{rec.get('qq', '')}",
            f"- 糯糯对他的称呼：{rec.get('name', '')}",
            f"- 关系：{rec.get('relation') or '未填写'}",
        ]
        note = str(rec.get("note") or "").strip()
        if note:
            lines += ["", "备注：", note]
        stamp = time.strftime("%Y-%m-%d", time.localtime(float(rec.get("note_ts") or 0)))
        lines += ["", f"（备注更新：{stamp}）"]
        return "\n".join(lines)

    @staticmethod
    def _own_doc_text(rec: dict) -> str:
        lines = [
            f"# 我的朋友：{rec.get('name', '')}",
            f"- 我这么叫他/她：{rec.get('name', '')}",
            f"- 关系：{rec.get('relation') or '还没弄清楚'}",
        ]
        note = str(rec.get("note") or "").strip()
        if note:
            lines += ["", "我记得的：", note]
        stamp = time.strftime("%Y-%m-%d", time.localtime(float(rec.get("note_ts") or 0)))
        lines += ["", f"（记于：{stamp}）"]
        return "\n".join(lines)

    async def _write_doc(self, rec: dict, kb_name: str = KB_NAME, prefix: str = "friend") -> None:
        try:
            kb = await self.context.kb_manager.get_kb_by_name(kb_name)
        except Exception as exc:
            logger.warning(f"[朋友档案] 知识库不可用：{exc}")
            return
        if kb is None:
            logger.warning(f"[朋友档案] 知识库「{kb_name}」不存在")
            return
        old = rec.get("doc_id")
        if old:
            try:
                await kb.delete_document(old)
            except Exception as exc:
                logger.warning(f"[朋友档案] 删除旧文档失败（继续重建）：{exc}")
        try:
            text = self._own_doc_text(rec) if prefix == "myfriend" else self._doc_text(rec)
            key = rec.get("qq") or rec.get("name") or "x"
            doc = await kb.upload_document(
                file_name=f"{prefix}-{key}.md",
                file_content=text.encode("utf-8"),
                file_type="md",
            )
            rec["doc_id"] = getattr(doc, "doc_id", None)
        except Exception as exc:
            logger.warning(f"[朋友档案] 写入知识库失败：{exc}")

    # ------------------------------------------------------------------ 会话门禁

    def _session_allowed(self, event) -> bool:
        """群聊默认不参与；私聊（白名单会话）才生效。"""
        cfg = getattr(self, "config", None) or {}
        if cfg.get("allow_group", False):
            return True
        try:
            message_type = event.get_message_type()
        except Exception:
            return True
        name = (getattr(message_type, "name", "") or str(message_type)).upper()
        return "GROUP" not in name

    # ------------------------------------------------------------------ 操作

    async def upsert(self, qq: str, name: str, relation: str = "", note: str = "") -> dict:
        qq = str(qq).strip()
        name = str(name).strip()
        friends = self.store.setdefault("friends", {})
        rec = friends.get(qq) or {"qq": qq, "note_ts": 0}
        rec["qq"] = qq
        rec["name"] = name
        if relation:
            rec["relation"] = str(relation).strip()
        if note:
            rec["note"] = str(note).strip()
            rec["note_ts"] = time.time()
        rec.setdefault("relation", "")
        rec.setdefault("note", "")
        friends[qq] = rec
        self._save()
        await self._write_doc(rec)
        return rec

    def listing(self) -> str:
        friends = self.store.get("friends") or {}
        if not friends:
            return "爸爸的亲朋还是空的。用 `/朋友 加 <QQ> <称呼> [关系]` 加人。"
        lines = [f"共 {len(friends)} 位朋友："]
        for rec in friends.values():
            rel = f"，{rec['relation']}" if rec.get("relation") else ""
            lines.append(f"· {rec.get('name')}（{rec.get('qq')}{rel}）")
        return "\n".join(lines)

    async def remove(self, qq: str) -> bool:
        friends = self.store.get("friends") or {}
        rec = friends.pop(str(qq).strip(), None)
        if not rec:
            return False
        if rec.get("doc_id"):
            kb = await self._kb()
            if kb:
                try:
                    await kb.delete_document(rec["doc_id"])
                except Exception as exc:
                    logger.warning(f"[朋友档案] 删除文档失败：{exc}")
        self._save()
        return True

    # ------------------------------------------------------------------ 压缩

    async def _summarize(self, note: str) -> str:
        prompt = (
            "把下面关于一位朋友的备注压缩成一句不超过 40 字的话，"
            "只保留长期有用的事实（喜好、习惯、关系、忌讳），不要评论、不要寒暄：\n"
            + note
        )
        try:
            from astrbot.core.agent.message import Message, TextPart

            resp = await self.context.llm_generate(
                contexts=[Message(role="user", content=[TextPart(text=prompt)])],
                max_tokens=120,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text or note[:60]
        except Exception as exc:
            logger.warning(f"[朋友档案] 摘要失败，退化为截断：{exc}")
            return note[:60]

    async def _maybe_compact(self) -> None:
        meta = self.store.setdefault("meta", {"last_compact": 0})
        if time.time() - float(meta.get("last_compact") or 0) < WEEK_SECONDS:
            return
        changed = 0
        for rec in (self.store.get("friends") or {}).values():
            note = str(rec.get("note") or "").strip()
            if not note:
                continue
            if time.time() - float(rec.get("note_ts") or 0) < WEEK_SECONDS:
                continue
            summary = await self._summarize(note)
            if summary and summary != note:
                rec["note"] = summary
                rec["note_ts"] = time.time()
                await self._write_doc(rec)
                changed += 1
        meta["last_compact"] = time.time()
        self._save()
        if changed:
            logger.info(f"[朋友档案] 七天压缩完成，更新了 {changed} 位朋友的备注")

    async def _compact_loop(self) -> None:
        while True:
            try:
                await self._maybe_compact()
            except Exception as exc:
                logger.warning(f"[朋友档案] 压缩任务出错：{exc}")
            await asyncio.sleep(COMPACT_CHECK_INTERVAL)

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._compact_loop())
        logger.info(
            f"[朋友档案] 已加载，当前 {len(self.store.get('friends') or {})} 位朋友"
        )

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    # ------------------------------------------------------------------ 命令

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("朋友")
    async def friend_command(self, event: AstrMessageEvent):
        """朋友档案：/朋友 加 <QQ> <称呼> [关系] | /朋友 列 | /朋友 删 <QQ> | /朋友 压缩"""
        text = (getattr(event, "message_str", "") or "").strip()
        if not self._session_allowed(event):
            yield event.plain_result("这个只在私聊生效，群里不处理。")
            return
        parts = text.split()
        action = parts[1] if len(parts) > 1 else "列"

        if action in ("列", "列表", "list"):
            yield event.plain_result(self.listing())
            return

        if action in ("加", "添加", "记"):
            if len(parts) < 4:
                yield event.plain_result("格式：/朋友 加 <QQ号> <糯糯对他的称呼> [关系]")
                return
            qq, name = parts[2], parts[3]
            relation = " ".join(parts[4:]).strip()
            if not qq.isdigit() or not 5 <= len(qq) <= 15:
                yield event.plain_result("QQ 号看起来不对，只要数字。")
                return
            rec = await self.upsert(qq, name, relation)
            rel = f"，关系：{rec['relation']}" if rec.get("relation") else ""
            yield event.plain_result(f"已记下：{rec['name']}（{rec['qq']}{rel}）")
            return

        if action in ("删", "删除", "移除"):
            if len(parts) < 3:
                yield event.plain_result("格式：/朋友 删 <QQ号>")
                return
            ok = await self.remove(parts[2])
            yield event.plain_result("已删除。" if ok else "档案里没有这个 QQ。")
            return

        if action in ("压缩", "整理"):
            await self._maybe_compact()
            yield event.plain_result("压缩检查已执行（距上次满 7 天才真正摘要）。")
            return

        yield event.plain_result("用法：/朋友 加 <QQ> <糯糯对他的称呼> [关系] | /朋友 列 | /朋友 删 <QQ>")

    # ------------------------------------------------------------------ LLM 工具

    @filter.llm_tool(name="remember_friend")
    async def remember_friend(
        self,
        event: AstrMessageEvent,
        qq: str,
        name: str,
        relation: str = "",
        note: str = "",
    ):
        """把一位朋友记进朋友档案。当主人说“这是我朋友 XX，QQ 是 xxx”时调用。

        Args:
            qq(string): 朋友的 QQ 号，纯数字
            name(string): 主人对这位朋友的称呼
            relation(string): 与主人的关系，例如 室友、同事、网友
            note(string): 可选备注，例如喜好、忌讳
        """
        qq = str(qq).strip()
        if not self._session_allowed(event):
            yield "这个只在私聊生效，群聊里不记录。"
            return
        if not qq.isdigit():
            yield "QQ 号必须是纯数字，没有记录。"
            return
        rec = await self.upsert(qq, str(name).strip(), relation, note)
        yield f"已记入爸爸的亲朋：{rec['name']}（{rec['qq']}）"

    @filter.llm_tool(name="remember_my_friend")
    async def remember_my_friend(
        self,
        event: AstrMessageEvent,
        name: str,
        relation: str = "",
        note: str = "",
    ):
        """把你（糯糯）自己认识的人记进你的朋友本。只有你觉得对方算你的朋友时才记。

        Args:
            name(string): 你平时怎么叫他/她，比如「小周」「春竹」
            relation(string): 什么关系，比如 群友、网友、常聊天的
            note(string): 你记得的关于他的事，一两句
        """
        if not self._session_allowed(event):
            yield "群里不记这个。"
            return
        name = str(name or "").strip()[:30]
        if not name:
            yield "没名字，记不了。"
            return
        mine = self.store.setdefault("my_friends", {})
        rec = mine.get(name) or {"name": name, "qq": "", "note_ts": 0}
        if relation:
            rec["relation"] = str(relation).strip()[:30]
        if note:
            rec["note"] = str(note).strip()[:200]
            rec["note_ts"] = time.time()
        mine[name] = rec
        self._save()
        await self._write_doc(rec, KB_OWN, "myfriend")
        yield f"记好了，{name} 现在是我朋友了。"

    # ------------------------------------------------------------------ 注入

    @filter.on_llm_request()
    async def inject_friends(self, event: AstrMessageEvent, req):
        if not self._session_allowed(event):
            return
        friends = self.store.get("friends") or {}
        if not friends:
            return
        lines = []
        for rec in list(friends.values())[:40]:
            rel = f"，关系：{rec['relation']}" if rec.get("relation") else ""
            lines.append(f"- {rec.get('name')}：QQ {rec.get('qq')}{rel}")
        block = (
            "\n\n【主人亲自登记的朋友】\n"
            + "\n".join(lines)
            + "\n（这些是主人确认过的朋友，提到对应 QQ 或称呼时按此对应；名单外的人不要编造关系。）"
        )
        req.system_prompt = (req.system_prompt or "") + block
