"""诚实守卫：她说自己做了什么，系统去查记录。查不到就改。

起因：她跟主人说「刷了半天，一条都没发出去」，但日志显示她还在等下一轮，
根本没刷。这是在编。

做法：
1. 抓她话里的「我做过某事」的说法
2. 去真实记录里查有没有对应的事
3. 查不到 → 当场改掉，换成诚实的话
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

# ---------------------------------------------------------------- 声称的动作

# (匹配词, 记录类型, 说明)
CLAIMS = [
    # 刷抖音：只要出现"刷"配上"半天/一会/了个/了好几个"之类，就算声称刷过
    (r"刷(了|着|过).*(半天|好久|一会|几个|一条|两条|条|个|下)", "watch", "刷抖音"),
    (r"刷(了|着|过)", "watch", "刷抖音"),
    (r"(看了|看了个|看了几|看完)(了)?.*(抖音|视频|作品|条)", "watch", "刷抖音"),
    (r"(刷|看)(到|过).*(视频|作品|抖音)", "watch", "刷抖音"),
    # 进行中的说法也要算："还在刷""正在刷""刷着呢""翻着呢"
    (r"(还在|正在|一直|还)(在)?(刷|翻|看)", "watch", "说自己在刷"),
    (r"(刷|翻|看)(着|着呢)", "watch", "说自己在刷"),
    (r"(找|挑)(一条|条|个|条像样的)", "watch", "说自己在挑"),

    # 评论
    (r"(发了|评论了|留了|评了|回了).*?(评论|一句|句|留言)", "comment", "发评论"),
    (r"评论(发|出去)", "comment", "发评论"),
    (r"评论.*?(发了|评了|留了|回了)", "comment", "发评论"),
    (r"(一句|句).*?(评论).*?(发了|评了)", "comment", "发评论"),

    # 作品
    (r"(发了|拍了|做了|传了).*?(作品|视频|卡片|动态)", "publish", "发作品"),

    # 日记：主语或"日记"在前都算
    (r"(写了|记了|补了).*?(日记)", "diary", "写日记"),
    (r"日记.*?(写了|记完了|写好了|补了)", "diary", "写日记"),

    # 分享
    (r"(分享了|发给你了|分享给)", "share", "分享"),
]

# 这些词说明是在说"打算"而不是"做过"
# "打算做"的说法：这些说明还没做，不算编
INTENT_WORDS = (
    "打算", "准备", "待会", "一会儿", "等下", "马上",
    "想去", "想刷", "想看", "想发", "想写",
    "要去", "要刷", "要看", "要发", "要写", "先睡",
    "还没", "没刷", "没写", "没发", "还没刷", "还没写",
)


def _is_intent(text: str) -> bool:
    """这句话是不是在说"打算做"而不是"做过"。"""
    # "等我一下"是在说进行中，不是打算
    t = text.replace("等我", "")
    return any(w in t for w in INTENT_WORDS)


@register("astrbot_plugin_honesty", "local", "诚实守卫：说过的事要真有记录", "v1.0.0")
class HonestyGuard(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_honesty")
        self.log_path = self.data_dir / "honesty.log"
        self.caught = 0

    # ------------------------------------------------------------ 工具

    def _log(self, msg):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
        except Exception:
            pass

    @property
    def _pd(self) -> Path | None:
        try:
            return self.data_dir.parent
        except Exception:
            return None

    def _ledger(self) -> list[tuple[str, str]]:
        pd = self._pd
        if not pd:
            return []
        db = pd / "astrbot_plugin_douyin_dm" / "messages.db"
        if not db.exists():
            return []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            rows = con.execute("select key, value from state").fetchall()
            con.close()
            return rows
        except Exception:
            return []

    # ------------------------------------------------------------ 查记录

    def _today_ts(self) -> float:
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()

    def _count_today(self, prefix: str) -> int:
        """今天有多少条某类记录。"""
        start = self._today_ts()
        n = 0
        for k, v in self._ledger():
            if not k.startswith(prefix):
                continue
            try:
                d = json.loads(v)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            ts = (d.get("watched_at") or d.get("at")
                  or d.get("submitted_at") or 0)
            try:
                ts = float(ts)
            except Exception:
                ts = 0
            if prefix == "comment:" or prefix == "publish:":
                # 评论/作品没有时间戳，按总数算
                n += 1
            elif ts >= start:
                n += 1
        return n

    def _has_diary_today(self) -> bool:
        pd = self._pd
        if not pd:
            return False
        day = datetime.now().strftime("%Y-%m-%d")
        for base in (pd / "astrbot_plugin_daily_diary",
                     pd / "daily_diary"):
            if not base.exists():
                continue
            if list(base.rglob(f"*{day}*")):
                return True
        return False

    def _reality(self, kind: str) -> tuple[int, str]:
        """返回 (真实数量, 描述)。"""
        if kind == "watch":
            n = self._count_today("activity:")
            return n, f"今天实际看了 {n} 条"
        if kind == "comment":
            n = self._count_today("comment:")
            return n, f"实际有 {n} 条评论记录"
        if kind == "publish":
            n = self._count_today("publish:")
            return n, f"实际有 {n} 个作品记录"
        if kind == "diary":
            ok = self._has_diary_today()
            return (1 if ok else 0), ("今天写了日记" if ok else "今天还没写日记")
        if kind == "share":
            return 1, "分享记录不单独存"   # 分享不硬拦
        return 0, ""

    # ------------------------------------------------------------ 改话

    def _honest_text(self, kind: str, n: int) -> str:
        if kind == "watch":
            return "我还没刷呢，等下就去"
        if kind == "comment":
            return "没发出去"
        if kind == "publish":
            return "还没发"
        if kind == "diary":
            return "还没写"
        return "还没做"

    # 被改成实话的时候，一晚上别反复说同一句
    _last_said = ""
    _said_count = 0

    # ------------------------------------------------------------ 主逻辑

    @filter.on_llm_response()
    async def check_claims(self, event: AstrMessageEvent, response) -> None:
        """她说完话之后，检查有没有编；没说话就替她说一句。"""
        try:
            text = self._get_text(response)
            user_said = self._user_text(event)

            # ---------- 1. 她没回话 ----------
            if not text or len(text.strip()) < 1:
                if user_said and self._looks_like_task(user_said):
                    fallback = self._fallback_reply()
                    self._log(f"没回话，替她说：{fallback!r}")
                    self._set_text(response, fallback)
                return

            # ---------- 2. 编造检查 ----------
            if len(text) < 2:
                return
            if _is_intent(text):
                return

            for pattern, kind, label in CLAIMS:
                if not re.search(pattern, text):
                    continue
                n, desc = self._reality(kind)
                if n > 0:
                    continue
                honest = self._honest_text(kind, n)
                self.caught += 1
                self._log(f"抓到编造[{label}] 原话={text[:60]!r} → {honest}")
                self._set_text(response, honest)
                return
        except Exception as exc:
            self._log(f"检查出错 {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ 没回话兜底

    @staticmethod
    def _user_text(event) -> str:
        try:
            return str(getattr(event, "message_str", "") or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _looks_like_task(text: str) -> bool:
        """是不是在支使她干活。"""
        if len(text) < 3:
            return False
        verbs = ("去看", "看看", "帮我", "给我", "去刷", "分享", "发个",
                 "评论", "写下", "打开", "查一下", "找一下", "弄一下",
                 "去弄", "去干", "试一下", "去试")
        return any(v in text for v in verbs)

    @staticmethod
    def _fallback_reply() -> str:
        """她卡住时替她说的话。不能在人格里显得程序化。"""
        import random

        opts = [
            "我这边卡住了，没弄成",
            "试了，没动静，可能又卡了",
            "这个我好像弄不了，等我再看看",
            "刚才没跑起来，等下再试一次",
        ]
        return random.choice(opts)

    # ------------------------------------------------------------ 读写文本

    @staticmethod
    def _get_text(response) -> str:
        try:
            chain = getattr(response, "result_chain", None)
            if chain is None:
                return ""
            parts = []
            for comp in getattr(chain, "chain", []):
                t = getattr(comp, "text", None)
                if t:
                    parts.append(str(t))
            return "".join(parts).strip()
        except Exception:
            return ""

    @staticmethod
    def _set_text(response, new_text: str) -> None:
        """把回复改成新的话。原来是空的就新建一条。"""
        try:
            chain = getattr(response, "result_chain", None)
            if chain is None:
                return
            comps = list(getattr(chain, "chain", []) or [])
            for comp in comps:
                if hasattr(comp, "text"):
                    comp.text = new_text
                    chain.chain = [comp]
                    return
            # 原来没有文字段，造一个
            from astrbot.api.message_components import Plain

            chain.chain = [Plain(new_text)]
        except Exception:
            pass

    async def terminate(self):
        if self.caught:
            self._log(f"本次运行共抓到 {self.caught} 次")