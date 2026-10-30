"""她想动电脑，得先问你。

流程：
1. 她调 request_pc_permission 说「我想干嘛」
2. 你收到一条请求，回复「同意」或「不同意」
3. 同意后，接下来 10 分钟内她能动手；过期自动收回
"""

from __future__ import annotations

import datetime
import json
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.event import MessageChain

DEFAULT_MINUTES = 10
MAX_MINUTES = 60


@register(
    "astrbot_plugin_nuonuo_ask",
    "local",
    "动电脑前先问一声",
    "v1.0.0",
)
class AskBeforeTouchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_nuonuo_ask")
        self.state_path = self.data_dir / "state.json"
        self.log_path = self.data_dir / "ask.log"
        self.state = self._load()

    # ---------------- 状态 ----------------
    def _load(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text("utf-8"))
        except Exception:
            d = {}
        d.setdefault("granted_until", 0.0)
        d.setdefault("pending", None)
        d.setdefault("owner_session", "")
        return d

    def _save(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8")
        except Exception:
            pass

    def _log(self, text: str) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
        except Exception:
            pass

    def _cfg(self, key, default=None):
        try:
            cfg = getattr(self, "config", None) or {}
            v = cfg.get(key)
            if v not in (None, ""):
                return v
        except Exception:
            pass
        return default

    def _is_recently_granted(self) -> bool:
        return time.time() < float(self.state.get("granted_until") or 0)

    def _remaining_min(self) -> int:
        left = float(self.state.get("granted_until") or 0) - time.time()
        return max(0, int(left // 60))

    # ---------------- 她来申请 ----------------
    @filter.llm_tool(name="ask_to_use_pc")
    async def ask_to_use_pc(self, event: AstrMessageEvent, what: str, why: str = ""):
        """想动电脑之前先问一声：说明你想干什么、为什么。

        Args:
            what(string): 你想做什么，比如「打开记事本」「看看桌面上有什么」
            why(string): 为什么要做，可留空
        """
        if self._is_recently_granted():
            yield f"已经问过了，还能动 {self._remaining_min()} 分钟，我自己来就行。"
            return

        what = str(what or "").strip()
        if not what:
            yield "先想清楚要干嘛再问呀。"
            return
        if len(what) > 100:
            what = what[:100]

        # 记住这是哪个会话，方便批准后回这个会话
        try:
            self.state["owner_session"] = str(event.unified_msg_origin or "")
        except Exception:
            pass
        self.state["pending"] = {
            "what": what,
            "why": str(why or "")[:100],
            "at": time.time(),
        }
        self._save()
        self._log(f"申请 {what}")

        yield (f"我问过爸爸了——我想{what}"
               + (f"，{why}" if why else "")
               + "。等他答应。")

    # ---------------- 主人批准 ----------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("同意")
    async def approve(self, event: AstrMessageEvent):
        """同意：批准她刚才的申请。"""
        parts = (getattr(event, "message_str", "") or "").strip().split()
        minutes = DEFAULT_MINUTES
        if len(parts) > 1:
            try:
                minutes = max(1, min(MAX_MINUTES, int(parts[1])))
            except ValueError:
                pass

        pending = self.state.get("pending")
        self.state["granted_until"] = time.time() + minutes * 60
        self.state["pending"] = None
        self._save()
        self._log(f"批准 {minutes} 分钟")

        what = (pending or {}).get("what", "动手")
        yield event.plain_result(
            f"好，{what}去吧。给你 {minutes} 分钟，别乱来。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("不同意")
    async def deny(self, event: AstrMessageEvent):
        """不同意：拒绝她刚才的申请。"""
        pending = self.state.get("pending")
        self.state["pending"] = None
        self._save()
        self._log("拒绝")
        what = (pending or {}).get("what", "动手")
        yield event.plain_result(f"不行，别{what}了。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("收手")
    async def revoke(self, event: AstrMessageEvent):
        """收手：立刻收回动手权限。"""
        self.state["granted_until"] = 0
        self.state["pending"] = None
        self._save()
        self._log("收回")
        yield event.plain_result("手收回来了，现在不能动了。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("手")
    async def status(self, event: AstrMessageEvent):
        """手：看看她现在能不能动电脑。"""
        if self._is_recently_granted():
            yield event.plain_result(
                f"她能动，还剩 {self._remaining_min()} 分钟。")
        elif self.state.get("pending"):
            p = self.state["pending"]
            yield event.plain_result(
                f"她在申请：{p.get('what')}。\n回「同意」或「不同意」。")
        else:
            yield event.plain_result("她现在不能动电脑，也没在申请。")

    # ---------------- 给 pc_hands 用的检查接口 ----------------
    async def terminate(self):
        pass