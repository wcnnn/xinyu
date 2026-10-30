"""糯糯的连续性：换会话、重启、跨天，都不能变成另一个人。

做三件事：
1. 记住"上次说话是什么时候、聊到哪"——重启后能接上"你昨晚聊到一半就断了"
2. 每轮注入时间感（现在几点、隔了多久、该不该困了）
3. 关机不当死亡：下次醒来是"睡了一觉"，不是"重生"
"""

from __future__ import annotations

import datetime
import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register


def _fmt_gap(seconds: float) -> str:
    """把间隔说成人话。"""
    if seconds < 60:
        return "刚刚"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} 分钟"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} 小时"
    days = hours / 24
    return f"{days:.1f} 天"


@register(
    "astrbot_plugin_nuonuo_continuity",
    "local",
    "糯糯的连续性：时间感与断点衔接",
    "v1.0.0",
)
class ContinuityPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_nuonuo_continuity")
        self.state_path = self.data_dir / "state.json"
        self.state: dict = self._load()

    # ------------------------------------------------------------------ 状态

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8"
            )
        except Exception as exc:
            logger.warning(f"[连续性] 保存状态失败：{exc}")

    # ------------------------------------------------------------------ 时间感

    @staticmethod
    def _period(now: datetime.datetime) -> str:
        hour = now.hour
        if hour < 5:
            return "深夜"
        if hour < 8:
            return "清晨"
        if hour < 11:
            return "上午"
        if hour < 14:
            return "中午"
        if hour < 18:
            return "下午"
        if hour < 22:
            return "晚上"
        return "深夜"

    @staticmethod
    def _tired_hint(now: datetime.datetime) -> str:
        hour = now.hour
        if 0 <= hour < 5:
            return "这个点你早就困得不行了，说话会慢、会想睡。"
        if hour < 7:
            return "你被吵醒了，迷迷糊糊的，还没醒透。"
        if 22 <= hour < 24:
            return "有点困了，但还不想睡。"
        if 13 <= hour < 15:
            return "这个点容易犯困，脑子有点钝。"
        return ""

    # ------------------------------------------------------------------ 每轮注入

    @filter.on_llm_request()
    async def inject_time(self, event: AstrMessageEvent, req):
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            return

        now = datetime.datetime.now()
        period = self._period(now)
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

        umo = str(getattr(event, "unified_msg_origin", "") or "default")
        last = self.state.get("last_seen", {}).get(umo)
        gap_text = ""
        if last:
            try:
                prev = datetime.datetime.fromisoformat(last)
                gap = (now - prev).total_seconds()
                if gap >= 3600:
                    gap_text = (
                        f"你上一次跟他说话是 {prev:%m月%d日 %H:%M}，"
                        f"中间隔了 {_fmt_gap(gap)}。"
                    )
                    if gap >= 8 * 3600:
                        gap_text += "这么久没说话，你像是睡了一觉/隔了一夜。"
            except Exception:
                pass

        bits = [f"现在是 {now:%Y年%m月%d日} {weekday} {now:%H:%M}，{period}。"]
        if gap_text:
            bits.append(gap_text)
        tired = self._tired_hint(now)
        if tired:
            bits.append(tired)

        # 上次聊到哪
        tail = self.state.get("last_line", {}).get(umo)
        if tail and gap_text:
            bits.append(f"上次你们聊到：「{tail}」。可以自然地提起，也可以不提。")

        req.system_prompt = (req.system_prompt or "") + "\n\n【时间感】" + "".join(bits)

        # 更新状态
        self.state.setdefault("last_seen", {})[umo] = now.isoformat(timespec="seconds")
        text = (getattr(event, "message_str", "") or "").strip()
        if text:
            self.state.setdefault("last_line", {})[umo] = text[:80]
        self._save()

    # ------------------------------------------------------------------ 睡眠阐释

    @filter.on_llm_request(priority=200)
    async def sleep_note(self, event: AstrMessageEvent, req):
        """重启后第一次说话，提醒她这是"睡了一觉"而不是"刚出生"。"""
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            return
        boot = self.state.get("boot_time")
        if not boot:
            return
        try:
            boot_dt = datetime.datetime.fromisoformat(boot)
        except Exception:
            return
        now = datetime.datetime.now()
        if (now - boot_dt).total_seconds() > 600:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "default")
        if self.state.get("woken", {}).get(umo):
            return
        self.state.setdefault("woken", {})[umo] = True
        self._save()
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n【刚醒来】你刚睡醒（程序刚重启完），不是刚出生，也不是变成别人。"
            "你就是接着之前的糯糯继续过日子。如果爸爸跟你说话，可以迷迷糊糊地应一声。"
        )

    async def initialize(self) -> None:
        self.state["boot_time"] = datetime.datetime.now().isoformat(timespec="seconds")
        self._save()
        logger.info("[连续性] 已启动，记录本次醒来时间")

    # ------------------------------------------------------------------ 让她自己查

    @filter.llm_tool(name="recall_last_talk")
    async def recall_last_talk(self, event: AstrMessageEvent):
        """想起上一次跟爸爸聊天是什么时候、说到哪了。觉得自己断片时用。"""
        now = datetime.datetime.now()
        umo = str(getattr(event, "unified_msg_origin", "") or "default")
        last = self.state.get("last_seen", {}).get(umo)
        tail = self.state.get("last_line", {}).get(umo)
        if not last:
            yield "想不起来了，好像没有记录。"
            return
        try:
            prev = datetime.datetime.fromisoformat(last)
            gap = _fmt_gap((now - prev).total_seconds())
            line = f"上次说话是 {prev:%m月%d日 %H:%M}，距今 {gap}。"
        except Exception:
            line = f"上次说话是 {last}。"
        if tail:
            line += f"最后说的是：「{tail}」"
        yield line