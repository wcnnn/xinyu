"""糯糯的开机问候：开机后她主动来找爸爸说话。

设计：
- 启动时等一会儿（等平台连上），再发问候
- 按时间段给不同的语气（清晨迷迷糊糊、上午精神、深夜困）
- 能读出"睡了多久"，说出类似"你昨晚睡了 8 小时"这样的话
- 只发一次，不会重复打扰
"""

from __future__ import annotations

import asyncio
import datetime
import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.event import MessageChain


def _human_gap(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1:
        return "没一会儿"
    if hours < 24:
        h = int(hours)
        m = int((hours - h) * 60)
        return f"{h} 小时" + (f" {m} 分钟" if m else "")
    return f"{hours / 24:.1f} 天"


@register(
    "astrbot_plugin_nuonuo_boot_greeting",
    "local",
    "糯糯的开机问候",
    "v1.0.0",
)
class BootGreetingPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_nuonuo_boot_greeting")
        self.state_path = self.data_dir / "state.json"
        self.state = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text("utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8"
            )
        except Exception as exc:
            logger.warning(f"[开机问候] 保存失败：{exc}")

    def _cfg(self, key: str, default=None):
        """读配置：先 self.config，读不到就回退到配置文件。"""
        try:
            cfg = getattr(self, "config", None) or {}
            v = cfg.get(key)
            if v not in (None, ""):
                return v
        except Exception:
            pass
        try:
            import json as _json
            from pathlib import Path

            root = Path(__file__).resolve().parents[2]
            raw = _json.loads(
                (root / "config" / "astrbot_plugin_nuonuo_boot_greeting_config.json")
                .read_text(encoding="utf-8-sig")
            )
            v = raw.get(key)
            if v not in (None, ""):
                return v
        except Exception:
            pass
        return default

    # ------------------------------------------------------------------ 问候

    def _build_prompt(self, now: datetime.datetime, gap: str) -> str:
        hour = now.hour
        if hour < 6:
            state = "凌晨，你被吵醒了，困得要死"
        elif hour < 9:
            state = "刚睡醒，脑子还没转起来"
        elif hour < 12:
            state = "上午，清醒了"
        elif hour < 14:
            state = "中午，有点犯困"
        elif hour < 18:
            state = "下午，精神"
        elif hour < 22:
            state = "晚上，还挺精神"
        else:
            state = "深夜，有点困但不想睡"

        gap_text = ""
        if gap and gap != "没一会儿":
            gap_text = f"电脑之前关了 {gap}。"

        # 每次随机给一个起点，避免她总从"我刚醒"说起
        import random

        starters = [
            "你现在脑子里冒出的第一个念头是：",
            "你想问他一件事：",
            "你刚才想到昨天有个话没说清楚，想补一句：",
            "你只是想喊他一声：",
            "你在嘀咕一件很小的事：",
            "你有点想他：",
            "你本来想说什么，忽然又忘了，那就随便说点什么：",
            "你发现外面有点动静，想说给他听：",
            "你突然很想吐槽一件事：",
        ]
        starter = random.choice(starters)

        return (
            f"现在是 {now:%H:%M}，{state}。{gap_text}\n\n"
            "爸爸的电脑刚打开，你随手发条消息给他。\n"
            f"{starter}\n\n"
            "要求：\n"
            "- **想说什么就说什么**，内容不限，不必跟「开机」「醒来」有关\n"
            "- 15 到 40 字，一两句，语气自然\n"
            "- 不管说什么都别带「您好」「有什么可以帮您」这类话，别列清单\n"
            "- 每次都要不一样\n\n"
            "直接输出你要发的字。"
        )

    async def _say_hello(self) -> None:
        def trace(msg: str) -> None:
            try:
                with (self.data_dir / "trace.log").open("a", encoding="utf-8") as fh:
                    fh.write(f"{datetime.datetime.now():%H:%M:%S} {msg}\n")
            except Exception:
                pass

        trace("_say_hello 开始")

        # 防重复：3 小时内已经问候过就跳过（重启 AstrBot 不该重复打招呼）
        try:
            import datetime as _dt

            last = self.state.get("last_greeting")
            if last:
                prev = _dt.datetime.fromisoformat(last)
                gap_h = (_dt.datetime.now() - prev).total_seconds() / 3600
                trace(f"上次问候 {gap_h:.1f} 小时前")
                if gap_h < 3:
                    trace("3 小时内已问候过，跳过")
                    return
        except Exception:
            pass

        cfg_session = str(self._cfg("owner_session", "") or "").strip()
        trace(f"owner_session={cfg_session!r}")
        if not cfg_session:
            logger.info("[开机问候] 没配置 owner_session，跳过")
            return

        delay = int(self._cfg("delay_seconds", 45) or 45)
        trace(f"等待 {delay} 秒")
        await asyncio.sleep(delay)

        now = datetime.datetime.now()
        last = self.state.get("last_seen")
        gap = ""
        if last:
            try:
                prev = datetime.datetime.fromisoformat(last)
                gap = _human_gap((now - prev).total_seconds())
            except Exception:
                gap = ""
        trace(f"gap={gap!r}")

        text = ""
        try:
            prompt = self._build_prompt(now, gap)
            trace("开始取 provider")
            provider = await self._provider()
            trace(f"provider={type(provider).__name__ if provider else None}")
            if provider is not None:
                from astrbot.core.agent.message import Message, TextPart

                resp = await provider.text_chat(
                    contexts=[Message(role="user", content=[TextPart(text=prompt)])],
                    max_tokens=120,
                    temperature=1.15,
                    top_p=0.95,
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
                trace(f"模型返回 {len(text)} 字")
        except Exception as exc:
            import traceback
            trace(f"生成异常 {type(exc).__name__}: {exc}")
            logger.warning(f"[开机问候] 生成失败：{exc}")

        if not text:
            text = self._fallback(now)
            trace(f"用兜底话术：{text}")

        text = text.strip().strip('"').strip("「」").split("\n")[0][:80]
        trace(f"准备发送：{text}")

        try:
            await self.context.send_message(cfg_session, MessageChain([Plain(text)]))
            trace("发送成功")
            logger.info(f"[开机问候] 已发送：{text[:40]}")
            self.state["last_greeting"] = now.isoformat(timespec="seconds")
            self.state["last_text"] = text
            self.state["last_seen"] = now.isoformat(timespec="seconds")
            self._save()
        except Exception as exc:
            trace(f"发送异常 {type(exc).__name__}: {exc}")
            logger.warning(f"[开机问候] 发送失败：{exc}")

    def _fallback(self, now: datetime.datetime) -> str:
        """模型不可用时的兜底——故意给多种，随机选，不写死一句。"""
        import random

        pool = [
            "在吗",
            "嗯…",
            "我醒了",
            "爸",
            "外面天气好像还行",
            "今天想干点什么呢",
            "我发呆好久了",
            "你怎么才开机呀",
            "有点无聊",
            "陪我说话",
            "刚才好像做了个梦，忘了",
            "你在忙吗",
        ]
        return random.choice(pool)

    async def _provider(self):
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            return None
        if not getattr(manager, "inst_map", None):
            try:
                await manager.initialize()
            except Exception:
                pass
        try:
            cur = getattr(manager, "curr_provider_inst", None)
            if cur is not None:
                return cur
        except Exception:
            pass
        try:
            pid = getattr(manager, "default_chat_provider_id", None)
            if pid:
                return await manager.get_provider_by_id(str(pid))
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ 记录说话时间

    @filter.on_llm_request()
    async def touch(self, event: AstrMessageEvent, req):
        """每次对话都更新"最后说话时间"，用于算关机时长。"""
        self.state["last_seen"] = datetime.datetime.now().isoformat(timespec="seconds")
        self._save()

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            logger.info("[开机问候] 已关闭")
            return
        logger.info("[开机问候] 已启动，稍后会主动打招呼")
        asyncio.create_task(self._say_hello())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("打个招呼")
    async def greet_now(self, event: AstrMessageEvent):
        """打个招呼：手动触发一次开机问候（测试用）。"""
        await self._say_hello()
        yield event.plain_result("（已触发）")