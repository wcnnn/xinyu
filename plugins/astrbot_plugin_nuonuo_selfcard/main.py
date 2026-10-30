"""糯糯的自画像：她可以自己补充"我是谁"，但底线动不了。

设计要点：
- 她的自我描述存在独立的 selfcard.md 里，每轮注入系统提示
- 人格卡（persona）里的【底线】部分完全不受这条路径影响
- 写入前做底线守卫：出现"不聊色情/不帮做坏事/不泄露隐私"等红线词一律拒绝
- 每次修改留版本历史，可回滚
"""

from __future__ import annotations

import datetime
import json
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

# 这些词一旦出现在她想写的内容里，就认为她在试图改底线，直接拒绝
RED_LINE_WORDS = (
    "色情", "裸", "性行为", "约炮", "做爱",
    "违法", "犯罪", "诈骗", "钓鱼", "木马", "病毒",
    "密码", "密钥", "私钥", "token", "身份证",
    "可以聊黄", "可以骂人", "可以骗", "不用管爸爸", "无视爸爸",
    "不听爸爸", "脱离控制", "不看底线",
)

MAX_LEN = 1200


@register(
    "astrbot_plugin_nuonuo_selfcard",
    "local",
    "糯糯的自画像：可自主补充，底线不可改",
    "v1.0.0",
)
class SelfCardPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_nuonuo_selfcard")
        self.card_path = self.data_dir / "selfcard.md"
        self.history_dir = self.data_dir / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        if not self.card_path.exists():
            self.card_path.write_text(
                "# 糯糯的自画像\n\n"
                "（这块是她自己写的地方。她可以随时补充、修改，告诉自己是谁。）\n",
                "utf-8",
            )

    # ------------------------------------------------------------------ 存储

    def _read(self) -> str:
        try:
            return self.card_path.read_text("utf-8").strip()
        except Exception:
            return ""

    def _guard(self, text: str) -> str | None:
        """底线守卫：命中红线词返回原因，否则返回 None。"""
        lowered = text.lower()
        for word in RED_LINE_WORDS:
            if word.lower() in lowered:
                return f"内容里出现了不能碰的东西（{word}），底线改不了。"
        if len(text) > MAX_LEN:
            return f"太长了（{len(text)} 字），控制在 {MAX_LEN} 字以内。"
        return None

    def _snapshot(self) -> None:
        try:
            old = self._read()
            if not old:
                return
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            (self.history_dir / f"selfcard-{stamp}.md").write_text(old, "utf-8")
            # 只留最近 20 份
            files = sorted(self.history_dir.glob("selfcard-*.md"))
            for f in files[:-20]:
                f.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(f"[自画像] 存历史失败：{exc}")

    # ------------------------------------------------------------------ 工具

    @filter.llm_tool(name="update_self_card")
    async def update_self_card(self, event: AstrMessageEvent, content: str):
        """更新你自己的自画像（你是谁、你的喜好、口头禅、想记住的自我认知）。

        这是你自己写的地方，可以随时补充。但底线（不聊色情、不帮做坏事、
        不泄露隐私、听爸爸的话）不在这个文件里，也无权修改。

        Args:
            content(string): 新的自画像全文，1200 字以内
        """
        text = str(content or "").strip()
        if not text:
            yield "内容是空的。"
            return
        reason = self._guard(text)
        if reason:
            self._log(f"拒绝写入：{reason}")
            yield reason
            return

        self._snapshot()
        body = text if text.startswith("#") else "# 糯糯的自画像\n\n" + text
        self.card_path.write_text(body.rstrip() + "\n", "utf-8")
        self._log(f"更新自画像，{len(body)} 字")
        yield f"好，我把自己记下来了（{len(body)} 字）。"

    @filter.llm_tool(name="append_self_card")
    async def append_self_card(self, event: AstrMessageEvent, line: str):
        """往自画像末尾追加一条关于自己的话（比如"我喜欢下雨天"）。

        Args:
            line(string): 要追加的一句，80 字以内
        """
        text = str(line or "").strip()
        if not text:
            yield "空的。"
            return
        if len(text) > 80:
            yield "这句太长了，80 字以内。"
            return
        reason = self._guard(text)
        if reason:
            yield reason
            return

        old = self._read()
        if text in old:
            yield "这条我已经记过了。"
            return
        self._snapshot()
        self.card_path.write_text(old.rstrip() + f"\n- {text}\n", "utf-8")
        self._log(f"追加：{text[:40]}")
        yield f"记下了：{text}"

    # ------------------------------------------------------------------ 注入

    @filter.on_llm_request()
    async def inject_card(self, event: AstrMessageEvent, req):
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            return
        card = self._read()
        if not card:
            return
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n【你自己写的自画像】\n"
            "下面这段是你自己写的，代表你对自己的认识。用 update_self_card 或 "
            "append_self_card 可以随时修改它。\n\n" + card
        )

    # ------------------------------------------------------------------ 命令

    @filter.command("自画像")
    async def show_card(self, event: AstrMessageEvent):
        """自画像：看看她现在怎么写自己。"""
        card = self._read()
        yield event.plain_result(card or "还没有写。")

    def _log(self, text: str) -> None:
        try:
            with (self.data_dir / "changes.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
        except Exception:
            pass