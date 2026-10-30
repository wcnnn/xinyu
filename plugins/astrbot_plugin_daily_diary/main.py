"""每日日记：白天悄悄收集素材，到点写两份小日记。

- 公开版（给爸爸看）：只写轻松日常，可发消息、可进知识库
- 私密版（只给她自己）：真实情绪、小秘密，**加密落盘、不进知识库**，
  只有她自己调用 recall_private 时才会解密读取；爸爸无权查看

落盘：
  diary/YYYY-MM-DD.md         公开版
  private/YYYY-MM-DD.mimi     私密版（Fernet 加密，密钥存在 key.bin，仅本机）
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import os
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register

PUBLIC_PROMPT = (
    "下面是今天发生的一些日常片段（都是你亲耳听到的）。\n"
    "请用你自己的语气，写一篇今天的小日记，150 字以内。\n"
    "只写你喜欢、开心、觉得温暖或有触动的事；没提到的不要编。\n"
    "不要流水账，不要总结对话，不要写成报告。\n"
    "第一人称，像偷偷写在笔记本上的那种。\n\n"
    "今天的片段：\n"
)

PRIVATE_PROMPT = (
    "下面是今天发生的一些日常片段（都是你亲耳听到的）。\n"
    "写一篇只有你自己能看到的日记，200 字以内。\n"
    "这篇没人会看，所以别写漂亮话：写你真实的感觉，包括委屈、赌气、"
    "小心眼、没敢说出口的话、偷偷的念头。\n"
    "第一人称，不用交代背景，像真的在跟自己说话。\n\n"
    "今天的片段：\n"
)

# 旧的单份日记仍在 diary/ 下的 PROMPT 兼容
PROMPT = PUBLIC_PROMPT


@register(
    "astrbot_plugin_daily_diary",
    "local",
    "每日日记：公开一份给爸爸，私密一份只给自己",
    "v2.0.0",
)
class DailyDiaryPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_daily_diary")
        self.diary_dir = self.data_dir / "diary"
        self.private_dir = self.data_dir / "private"
        self.diary_dir.mkdir(parents=True, exist_ok=True)
        self.private_dir.mkdir(parents=True, exist_ok=True)
        self.key_path = self.data_dir / "key.bin"
        self.records_path = self.data_dir / "records.json"
        self.records: dict[str, list[str]] = self._load_records()
        self._last_written = ""
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ 私密存储

    def _fernet(self):
        from cryptography.fernet import Fernet

        if not self.key_path.exists():
            self.key_path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(self.key_path, 0o600)
            except Exception:
                pass
        return Fernet(self.key_path.read_bytes().strip())

    def _archive_dirs(self) -> list[Path]:
        cfg = getattr(self, "config", None) or {}
        raw = str(cfg.get("archive_dir") or "").strip()
        if not raw:
            # 配置读不到时：归档到本插件数据目录下的 archive，不写死盘符
            try:
                raw = str(Path(__file__).resolve().parents[2] / "plugin_data"
                          / "astrbot_plugin_daily_diary" / "archive")
            except Exception:
                raw = ""
        return [Path(part.strip()) for part in raw.split(";") if part.strip()]

    def _write_archive(self, root: Path, day: str, body: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{day}.md").write_text(body, "utf-8")

    def _archive_private(self, day: str) -> None:
        """把加密副本和密钥一起备份，离线也还能解开。"""
        src = self.private_dir / f"{day}.mimi"
        if not src.exists():
            return
        for root in self._archive_dirs():
            try:
                private_dir = root / "私密日记"
                key_dir = root / "私密日记-密钥"
                private_dir.mkdir(parents=True, exist_ok=True)
                key_dir.mkdir(parents=True, exist_ok=True)
                (private_dir / f"{day}.mimi").write_bytes(src.read_bytes())
                (key_dir / "key.bin").write_bytes(self.key_path.read_bytes())
            except Exception as exc:
                logger.warning(f"[日记] 归档到 {root} 失败：{exc}")
                try:
                    (self.data_dir / "archive_error.log").write_text(
                        f"root={root}\nerror={type(exc).__name__}: {exc}\n", "utf-8"
                    )
                except Exception:
                    pass

    def _save_private(self, day: str, text: str) -> None:
        token = self._fernet().encrypt(text.encode("utf-8"))
        (self.private_dir / f"{day}.mimi").write_bytes(token)
        self._archive_private(day)

    def _read_private(self, day: str) -> str:
        path = self.private_dir / f"{day}.mimi"
        if not path.exists():
            return ""
        try:
            return self._fernet().decrypt(path.read_bytes()).decode("utf-8")
        except Exception as exc:
            logger.warning(f"[日记] 私密日记解密失败：{exc}")
            return ""

    # ------------------------------------------------------------------ 存储

    def _load_records(self) -> dict:
        try:
            data = json.loads(self.records_path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_records(self) -> None:
        try:
            keep_days = int(self._cfg("keep_days", 7) or 7)
            cutoff = (datetime.date.today() - datetime.timedelta(days=keep_days)).isoformat()
            # 只清理超过保留期的素材；近几天的留着，方便日记没写成时补写
            for key in list(self.records):
                if key < cutoff:
                    self.records.pop(key, None)
            self.records_path.write_text(
                json.dumps(self.records, ensure_ascii=False, indent=1), "utf-8"
            )
        except Exception as exc:
            logger.warning(f"[日记] 保存素材失败：{exc}")

    # ------------------------------------------------------------------ 采集

    @staticmethod
    def _is_group(event) -> bool:
        try:
            message_type = event.get_message_type()
        except Exception:
            return False
        return "GROUP" in (getattr(message_type, "name", "") or str(message_type)).upper()

    @filter.on_llm_request()
    async def collect(self, event: AstrMessageEvent, req):
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            return
        if cfg.get("private_only", True) and self._is_group(event):
            return
        text = (getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith("/") or len(text) > 300:
            return
        today = datetime.date.today().isoformat()
        bucket = self.records.setdefault(today, [])
        limit = int(cfg.get("max_lines", 120) or 120)
        if len(bucket) < limit:
            bucket.append(text.replace("\n", " "))
            self._save_records()

    # ------------------------------------------------------------------ 生成

    async def _provider_id(self) -> str:
        """找出一个运行时真实可用的 provider id。

        注意：AstrBot 的 provider_manager 用的 id 跟 cmd_config 里的 id 字段不一定相同，
        所以这里以 provider_manager 实际注册的为准。
        """
        cfg = getattr(self, "config", None) or {}
        explicit = str(cfg.get("provider_id") or "").strip()
        if explicit:
            return explicit

        manager = getattr(self.context, "provider_manager", None)
        if manager is not None:
            # 1) 默认对话模型
            try:
                pid = getattr(manager, "default_chat_provider_id", None)
                if pid:
                    return str(pid)
            except Exception:
                pass
            # 2) 当前实例
            try:
                cur = getattr(manager, "curr_provider_inst", None)
                pid = getattr(cur, "id", None) or getattr(
                    getattr(cur, "meta", None), "id", None
                )
                if pid:
                    return str(pid)
            except Exception:
                pass
            # 3) 已加载实例列表
            try:
                for inst in getattr(manager, "get_insts", lambda: [])() or []:
                    pid = getattr(inst, "id", None) or getattr(
                        getattr(inst, "meta", None), "id", None
                    )
                    if pid and "embedding" not in str(pid).lower():
                        return str(pid)
            except Exception:
                pass

        try:
            umo = str(cfg.get("owner_session") or "").strip()
            if umo:
                pid = await self.context.get_current_chat_provider_id(umo)
                if pid:
                    return str(pid)
        except Exception as exc:
            logger.warning(f"[日记] 取会话默认 provider 失败：{exc}")

        return ""

    def _inst_keys(self) -> list[str]:
        """provider_manager.inst_map 里真实注册的键，并写一份到 debug.txt。"""
        manager = getattr(self.context, "provider_manager", None)
        keys: list[str] = []
        detail = {}
        if manager is not None:
            inst = getattr(manager, "inst_map", None)
            detail["type"] = type(inst).__name__
            if isinstance(inst, dict):
                keys = [str(k) for k in inst.keys()]
            else:
                keys = [str(getattr(p, "id", "")) for p in inst or []]
            detail["keys"] = keys
            try:
                detail["manager_attrs"] = [a for a in dir(manager) if not a.startswith("_")][:40]
            except Exception:
                pass
        try:
            (self.data_dir / "debug.txt").write_text(
                json.dumps(detail, ensure_ascii=False, indent=1), "utf-8"
            )
        except Exception:
            pass
        return keys

    async def _generate(self, lines: list[str], prompt: str, limit: int = 500) -> str:
        from astrbot.core.agent.message import Message, TextPart

        material = "\n".join(f"- {line}" for line in lines[-60:])
        contexts = [Message(role="user", content=[TextPart(text=prompt + material)])]

        # 优先直接拿 provider 实例调用，绕开 llm_generate 的 ID 查找
        provider = await self._provider_instance()
        if provider is not None:
            resp = await provider.text_chat(contexts=contexts, max_tokens=limit)
            return (getattr(resp, "completion_text", "") or "").strip()

        # 退路：走 llm_generate
        provider_id = await self._provider_id()
        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            contexts=contexts,
            max_tokens=limit,
        )
        return (getattr(resp, "completion_text", "") or "").strip()

    async def _provider_instance(self):
        """拿到真正可用的对话 provider 实例。"""
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            return None

        cfg = getattr(self, "config", None) or {}
        explicit = str(cfg.get("provider_id") or "").strip()

        # 如果实例没加载，尝试加载一次
        if not getattr(manager, "inst_map", None):
            try:
                await manager.initialize()
                logger.info("[日记] provider_manager 重新初始化完成")
            except Exception as exc:
                logger.warning(f"[日记] provider_manager 初始化失败：{exc}")

        # 诊断：把 manager 的真实状态写下来
        try:
            info = {
                "inst_map_keys": list(getattr(manager, "inst_map", {}) or {}),
                "curr": getattr(getattr(manager, "curr_provider_inst", None), "id", None),
                "provider_insts": [
                    getattr(p, "id", None) for p in (getattr(manager, "provider_insts", []) or [])
                ],
                "default": getattr(manager, "default_chat_provider_id", None),
            }
            (self.data_dir / "debug2.txt").write_text(
                json.dumps(info, ensure_ascii=False, indent=1), "utf-8"
            )
        except Exception:
            pass

        # 1) 指定 ID
        if explicit:
            try:
                prov = await manager.get_provider_by_id(explicit)
                if prov is not None:
                    return prov
            except Exception:
                pass

        # 2) 当前实例
        try:
            cur = getattr(manager, "curr_provider_inst", None)
            if cur is not None:
                return cur
        except Exception:
            pass

        # 3) 会话默认
        try:
            umo = str(cfg.get("owner_session") or "").strip()
            prov = await self.context.get_using_provider_async(umo or None)
            if prov is not None:
                return prov
        except Exception:
            pass

        # 4) 已加载列表
        try:
            for prov in getattr(manager, "provider_insts", []) or []:
                kind = str(getattr(getattr(prov, "meta", None), "type", "") or "")
                if "embedding" not in kind.lower():
                    return prov
        except Exception:
            pass
        return None

    def _cfg(self, key: str, default=None):
        """读插件配置。self.config 有时读不到，回退到直接读配置文件。"""
        try:
            cfg = getattr(self, "config", None) or {}
            value = cfg.get(key)
            if value not in (None, ""):
                return value
        except Exception:
            pass
        try:
            root = Path(__file__).resolve().parents[2]
            raw = json.loads((root / "config" / "astrbot_plugin_daily_diary_config.json")
                             .read_text(encoding="utf-8-sig"))
            value = raw.get(key)
            if value not in (None, ""):
                return value
        except Exception:
            pass
        return default

    async def _store_kb(self, day: str, text: str) -> None:
        kb_name = str(self._cfg("kb_name", "") or "").strip()
        self._trace(f"_store_kb 进入 kb_name={kb_name!r}")
        if not kb_name:
            self._trace("kb_name 为空，跳过")
            return
        try:
            kb = await self.context.kb_manager.get_kb_by_name(kb_name)
            self._trace(f"取到知识库：{getattr(kb, 'kb_name', None) or kb}")
            if kb is None:
                logger.info(f"[日记] 知识库「{kb_name}」不存在，只存本地文件")
                self._trace(f"知识库「{kb_name}」不存在")
                return
            result = await kb.upload_document(
                file_name=f"diary-{day}.md",
                file_content=f"# {day} 的小日记\n\n{text}\n".encode("utf-8"),
                file_type="md",
            )
            self._trace(f"upload_document 返回：{result}")
        except Exception as exc:
            import traceback
            detail = traceback.format_exc()
            logger.warning(f"[日记] 写入知识库失败（继续写私密版）：{exc}")
            self._trace(f"知识库异常 {type(exc).__name__}: {exc} | {detail[-500:]}")

    def _trace(self, msg: str) -> None:
        try:
            with (self.data_dir / "trace.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.datetime.now():%H:%M:%S} {msg}\n")
        except Exception:
            pass

    async def write_diary(self, day: str, force: bool = False) -> str | None:
        self._trace(f"write_diary 进入 day={day} force={force} 素材={len(self.records.get(day) or [])}")
        lines = self.records.get(day) or []
        if not lines and not force:
            logger.info("[日记] 今天没有素材，跳过")
            self._trace("无素材，退出")
            return None

        # 1) 公开版：给爸爸看，可进知识库
        try:
            public_text = (
                await self._generate(lines, PUBLIC_PROMPT) if lines
                else "今天什么都没听到，就发了一整天呆。"
            )
        except Exception as exc:
            import traceback
            detail = traceback.format_exc()
            logger.warning(f"[日记] 公开版生成失败：{type(exc).__name__}: {exc} | {detail[-400:]}")
            try:
                (self.data_dir / "error.log").write_text(detail, "utf-8")
            except Exception:
                pass
            return None
        path = self.diary_dir / f"{day}.md"
        body = f"# {day} 的小日记\n\n{public_text}\n"
        path.write_text(body, "utf-8")
        self._trace("公开版已写入")

        # 归档放到后台线程，且不阻塞主流程（F 盘可能是慢速/网络盘）
        try:
            import asyncio as _aio
            roots = self._archive_dirs()
            self._trace(f"归档根目录={roots}")
            for root in roots:
                try:
                    await _aio.wait_for(_aio.to_thread(self._write_archive, root, day, body), timeout=8)
                    self._trace(f"归档公开版 -> {root}")
                except Exception as exc:
                    self._trace(f"归档失败 {root}: {type(exc).__name__} {exc}")
        except Exception as exc:
            self._trace(f"归档异常 {exc}")

        try:
            await _aio.wait_for(self._store_kb(day, public_text), timeout=20)
            self._trace("知识库写入完成")
        except Exception as exc:
            logger.warning(f"[日记] 写知识库失败（继续写私密版）：{exc}")
            self._trace(f"知识库失败 {type(exc).__name__} {exc}")

        self._trace("准备写私密版")
        # 2) 私密版：只给她自己，加密落盘、不进知识库、不发消息
        try:
            flag = self._cfg("private_diary", True)
            try:
                (self.data_dir / "cfg_dump.txt").write_text(
                    json.dumps({k: v for k, v in cfg.items()}, ensure_ascii=False, indent=1),
                    "utf-8",
                )
            except Exception:
                pass
            if flag and lines:
                private_text = await self._generate(lines, PRIVATE_PROMPT, 600)
                logger.info(f"[日记] 私密版生成长度={len(private_text or '')}")
                if private_text:
                    self._save_private(day, private_text)
                    logger.info("[日记] 私密版已落盘")
        except Exception as exc:
            import traceback
            detail = traceback.format_exc()
            logger.warning(f"[日记] 私密版生成失败：{exc}")
            try:
                (self.data_dir / "private_error.log").write_text(detail, "utf-8")
            except Exception:
                pass

        cfg = getattr(self, "config", None) or {}
        if cfg.get("send_to_owner", True):
            session = str(cfg.get("owner_session") or "").strip()
            if session:
                try:
                    await self.context.send_message(
                        session, MessageChain([Plain(f"今天的小日记写好了：\n\n{public_text}")])
                    )
                except Exception as exc:
                    logger.warning(f"[日记] 发送失败：{exc}")
        return public_text

    # ------------------------------------------------------------------ 定时

    async def _tick(self) -> None:
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            return
        raw = str(cfg.get("write_time") or "23:30")
        try:
            hh, mm = (int(part) for part in raw.split(":", 1))
        except Exception:
            hh, mm = 23, 30
        now = datetime.datetime.now()
        today = now.date().isoformat()

        # 先补之前几天（有素材但没写日记的）
        missing_days = []
        for day_key in sorted(self.records.keys()):
            if day_key >= today:
                continue
            if not (self.diary_dir / f"{day_key}.md").exists():
                missing_days.append(day_key)
        for day_key in missing_days:
            logger.info(f"[日记] 补写 {day_key}")
            self._trace(f"补写 {day_key}")
            try:
                await self.write_diary(day_key, force=True)
            except Exception as exc:
                self._trace(f"补写 {day_key} 失败：{exc}")

        if (now.hour, now.minute) < (hh, mm):
            return
        if self._last_written == today:
            return
        await self.write_diary(today)
        self._last_written = today

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as exc:
                logger.warning(f"[日记] 定时检查异常：{exc}")
            await asyncio.sleep(60)


    async def _recheck_startup(self) -> None:
        """启动时补写：已过写日记时间且今天还没写。"""
        await asyncio.sleep(8)
        try:
            await self._tick()
        except Exception as exc:
            logger.warning(f"[日记] 启动补写失败：{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日记")
    async def diary_command(self, event: AstrMessageEvent):
        """日记：/日记 写（现在补写今天的） | /日记 看 [日期]"""
        parts = (getattr(event, "message_str", "") or "").strip().split()
        action = parts[1] if len(parts) > 1 else "看"
        today = datetime.date.today().isoformat()

        if action in ("写", "补"):
            text = await self.write_diary(today, force=True)
            yield event.plain_result(text or "没写出来，可能是模型调用失败，稍后再试。")
            return

        day = parts[2] if len(parts) > 2 else today
        path = self.diary_dir / f"{day}.md"
        if path.exists():
            yield event.plain_result(path.read_text("utf-8")[:1500])
        else:
            yield event.plain_result(f"{day} 还没有日记。")

    # ------------------------------------------------------------------ 她自己的私密日记

    @filter.llm_tool(name="recall_private_diary")
    async def recall_private_diary(self, event: AstrMessageEvent, days_ago: str = "0"):
        """翻你自己写的那本私密日记（只有你能看，爸爸看不到）。

        Args:
            days_ago(string): 往前几天，0 是今天，1 是昨天，默认 0
        """
        try:
            offset = max(0, min(365, int(str(days_ago or "0").strip())))
        except Exception:
            offset = 0
        day = (datetime.date.today() - datetime.timedelta(days=offset)).isoformat()
        text = self._read_private(day)
        if not text:
            yield f"{day} 没有私密日记。"
            return
        yield f"{day} 你写下的：\n\n{text}"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日记测试")
    async def diary_test(self, event: AstrMessageEvent):
        """诊断：测试知识库写入链路。"""
        import sqlite3

        lines = []
        cfg = getattr(self, "config", None) or {}
        kb_name = str(cfg.get("kb_name") or "").strip()
        lines.append(f"配置里 kb_name = {kb_name!r}")

        try:
            kb = await self.context.kb_manager.get_kb_by_name(kb_name)
        except Exception as exc:
            lines.append(f"取知识库异常：{type(exc).__name__}: {exc}")
            yield event.plain_result("\n".join(lines))
            return

        if kb is None:
            lines.append(f"知识库「{kb_name}」取不到")
            yield event.plain_result("\n".join(lines))
            return

        lines.append(f"取到知识库：{getattr(kb, 'kb_name', '?')}")
        try:
            result = await kb.upload_document(
                file_name=f"test-{int(datetime.datetime.now().timestamp())}.md",
                file_content="# 测试\n\n这是一条知识库写入测试。\n".encode("utf-8"),
                file_type="md",
            )
            lines.append(f"写入返回：{result}")
        except Exception as exc:
            lines.append(f"写入异常：{type(exc).__name__}: {exc}")

        try:
            db = Path(self.data_dir).parents[3] / "knowledge_base" / "kb.db"
            con = sqlite3.connect(db)
            n = con.execute("select count(*) from kb_documents").fetchone()[0]
            lines.append(f"当前文档总数：{n}")
        except Exception as exc:
            lines.append(f"查库失败：{exc}")

        yield event.plain_result("\n".join(lines))

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        self._task2 = asyncio.create_task(self._recheck_startup())
        logger.info("[日记] 已启动，公开目录：%s 私密目录：%s" % (self.diary_dir, self.private_dir))

    async def terminate(self) -> None:
        for task in (getattr(self, "_task", None), getattr(self, "_task2", None)):
            if task:
                task.cancel()
