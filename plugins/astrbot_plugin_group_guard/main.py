"""群聊护栏：群里只闲聊，不接费算力的活儿。

私聊（白名单会话）完全不受影响。群聊里会在系统提示末尾追加一段规则：
- 不回答学术、作业、编程、专业问答、数学推导等需要深入思考的问题
- 不调用任何工具（可配置放开）
- 不展开长文，遇到这类问题用一两句轻巧推掉
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

DEFAULT_RULE = (
    "\n\n【群聊模式】现在是群聊，只参与日常闲聊。\n"
    "- 不回答学术、作业、编程、专业问答、数学推导、长篇分析这类需要深入思考的问题。\n"
    "- 不调用任何工具，不查资料，不展开长文。\n"
    "- 遇到这类问题，用一两句轻巧地推掉就好，比如「这个私下问我嘛」「群里说不清楚」。\n"
    "- 群里就聊点轻松的：吃了吗、在干嘛、好笑的事。\n"
)


@register(
    "astrbot_plugin_group_guard",
    "local",
    "群聊护栏：群里只闲聊，不接学术/作业/编程等费算力的问题",
    "v1.0.0",
)
class GroupGuardPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    def _is_group(self, event) -> bool:
        try:
            message_type = event.get_message_type()
        except Exception:
            return False
        name = (getattr(message_type, "name", "") or str(message_type)).upper()
        return "GROUP" in name

    @filter.on_llm_request()
    async def apply_group_rule(self, event: AstrMessageEvent, req):
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            return
        if not self._is_group(event):
            return

        rule = str(cfg.get("group_rule") or "").strip() or DEFAULT_RULE
        if not cfg.get("allow_tools_in_group", False):
            rule += "- 这一轮不要调用任何工具。\n"

        req.system_prompt = (req.system_prompt or "") + rule
