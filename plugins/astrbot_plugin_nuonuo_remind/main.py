"""糯糯的提醒：她答应过的事，到点真的会响。

以前人格里写着「你会定个提醒，答应了就真去定」，但她手上根本没有这个工具，
只剩下两条路——违背这条规则，或者假装定了。这个插件把那条规则补上。

存本地 sqlite，重启不丢。断电错过太久的标 missed，不事后补刀：
三点的提醒十点才发，不是提醒，是吓人。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import re
import sqlite3
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_nuonuo_remind"

# 过期超过这么久就不补了（补也晚了）
MAX_LATE = 6 * 3600
# 一个人最多挂多少个待办提醒
MAX_PENDING = 20


# ────────────────────────────────────────────── 时间解析

_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_PERIOD = r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|今晚|明晚)"


def _cn2int(text: str):
    """把「3」「三」「十二」「二十三」都变成整数。"""
    s = str(text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        hi = _CN_DIGIT.get(head, 1) if head else 1
        lo = _CN_DIGIT.get(tail, 0) if tail else 0
        return hi * 10 + lo
    if len(s) == 1:
        return _CN_DIGIT.get(s)
    return None


def _apply_period(word: str, hour: int) -> int:
    """「下午2点」→14 点。"""
    if not word:
        return hour
    if word == "凌晨" and hour == 12:
        return 0
    if word in ("下午", "傍晚", "晚上", "夜里", "今晚", "明晚") and hour < 12:
        return hour + 12
    if word == "中午" and hour < 11:
        return hour + 12
    return hour


def parse_when(text: str, base: dt.datetime | None = None):
    """把中文时间说法变成 datetime，认不出来返回 None。"""
    now = base or dt.datetime.now()
    raw = re.sub(r"\s+", " ", str(text or "").strip())
    if not raw:
        return None
    t = raw.replace("：", ":")

    # 1) 完整年月日 [时:分]
    m = re.search(
        r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*[日号]?"
        r"(?:[ T]*(\d{1,2})\s*[:时点]\s*(\d{1,2})?)?",
        t,
    )
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = _cn2int(m.group(4)) if m.group(4) else 9
        mi = _cn2int(m.group(5)) if m.group(5) else 0
        try:
            return dt.datetime(y, mo, d, hh, mi)
        except ValueError:
            return None

    # 2) 几月几号 [时段][几点]
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        hh, mi = 9, 0
        tm = re.search(_PERIOD + r"?\s*(\d{1,2})\s*[点时]\s*(\d{1,2})?", t)
        if tm and tm.group(2):
            hh = _cn2int(tm.group(2)) or 9
            hh = _apply_period(tm.group(1), hh)
            mi = int(tm.group(3)) if tm.group(3) else 0
        for year in (now.year, now.year + 1):
            try:
                out = dt.datetime(year, mo, d, hh, mi)
            except ValueError:
                return None
            if out > now:
                return out
        return None

    # 3) 本周/下周 星期几 [几点]  —— 必须排在「几点」前面
    m = re.search(r"(下|这|本)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])", t)
    if m:
        prefix, wd = m.group(1), m.group(2)
        idx = _WEEKDAY.get(wd)
        if idx is None:
            return None
        hh, mi = 9, 0
        tm = re.search(_PERIOD + r"?\s*(\d{1,2})\s*[点时]\s*(\d{1,2})?", t)
        if tm and tm.group(2):
            hh = _cn2int(tm.group(2)) or 9
            hh = _apply_period(tm.group(1), hh)
            mi = int(tm.group(3)) if tm.group(3) else 0
        monday = now.date() - dt.timedelta(days=now.weekday())
        target = monday + dt.timedelta(days=idx)
        if prefix == "下":
            target += dt.timedelta(days=7)
        out = dt.datetime.combine(target, dt.time(hh, mi))
        if out <= now:
            out += dt.timedelta(days=7)
        return out

    # 4) 相对时间：3 小时后 / 两天后 / 半小时后
    m = re.search(
        r"([0-9]{1,3}|[零一二两三四五六七八九十]{1,3})\s*(个)?\s*"
        r"(分钟|分|小时|钟头|天|日|周|星期|礼拜)\s*(?:之后|以后|后)",
        t,
    )
    if m:
        n = _cn2int(m.group(1))
        unit = m.group(3)
        if n is None:
            return None
        if unit in ("分钟", "分"):
            return now + dt.timedelta(minutes=n)
        if unit in ("小时", "钟头"):
            return now + dt.timedelta(hours=n)
        if unit in ("天", "日"):
            return now + dt.timedelta(days=n)
        return now + dt.timedelta(weeks=n)
    if re.search(r"半\s*(个)?\s*(小时|钟头)\s*(?:之后|以后|后)", t):
        return now + dt.timedelta(minutes=30)

    # 5) 今天/明天/后天 [时段] 几点 [几分]
    m = re.search(
        r"(今天|今晚|明天|明晚|后天|大后天)?\s*" + _PERIOD + r"?\s*"
        r"([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*[点时]"
        r"\s*(半|[0-9]{1,2}|[零一二三四五六七八九十]{1,3})?\s*分?",
        t,
    )
    if m:
        day_word, period, hh_s, mi_s = m.group(1), m.group(2), m.group(3), m.group(4)
        hh = _cn2int(hh_s)
        if hh is None:
            return None
        if mi_s == "半":
            mi = 30
        elif mi_s:
            mi = _cn2int(mi_s) or 0
        else:
            mi = 0
        if day_word in ("明天", "明晚"):
            off = 1
        elif day_word == "后天":
            off = 2
        elif day_word == "大后天":
            off = 3
        else:
            off = 0
        if day_word == "今晚":
            period = period or "晚上"
        if day_word == "明晚":
            period = period or "晚上"
        hh = _apply_period(period, hh)
        if not (0 <= hh <= 23 and 0 <= mi <= 59):
            return None
        day = (now + dt.timedelta(days=off)).date()
        out = dt.datetime.combine(day, dt.time(hh, mi))
        # 只对「9点」这种没说是哪天的说法往后顺延一天；
        # 「今晚8点」说晚了就是晚了，让调用方去报错，别偷偷改成明天。
        if out <= now and not day_word:
            out += dt.timedelta(days=1)
        return out

    return None


_PUNCT = "，。、！？…—－-·「」\"'（）()《》 \u3000"

_TIME_WORDS = re.compile(
    r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|今晚|明晚|今天|明天|明夜|后天|大后天"
    r"|下周|本周|这周|周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天]"
    r"|\d{4}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}"
    r"|\d{1,2}\s*[:：]\s*\d{1,2}"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{1,2}\s*[点时]\s*(半|\d{1,2})?\s*分?"
    r"|[零一二三四五六七八九十两]{1,3}\s*[点时]\s*(半)?\s*分?)"
)


def _core_of(what: str) -> str:
    """把时间词和标点去掉，只留下「到底要干什么」。"""
    s = str(what or "")
    s = _TIME_WORDS.sub("", s)
    return "".join(ch for ch in s if ch not in _PUNCT)


def _mentions(text: str, what: str) -> bool:
    """这句话有没有真的说出那件事？

    只看时间（「两点半了」「到点了」）不算——提醒了却不说是什么事，等于没提醒。
    """
    core = _core_of(what)
    t = str(text or "")
    if not core:
        return True  # 事情本身就是个时间，没别的可核对
    if len(core) <= 3:
        return core in t
    for i in range(len(core) - 2):
        if core[i : i + 3] in t:
            return True
    return False


def _fmt(when: dt.datetime) -> str:
    """给她看的说法：11月2日（周一）9:00，还有 2 天"""
    gap = when - dt.datetime.now()
    mins = int(gap.total_seconds() // 60)
    if mins < 0:
        left = "时间已经过了"
    elif mins < 60:
        left = f"还有 {mins} 分钟"
    elif mins < 60 * 24:
        left = f"还有 {mins // 60} 小时 {mins % 60} 分"
    else:
        left = f"还有 {mins // (60 * 24)} 天"
    wd = "周" + "一二三四五六日"[when.weekday()]
    return f"{when.month}月{when.day}日（{wd}）{when.hour}:{when.minute:02d}，{left}"


# ────────────────────────────────────────────── 插件


@register(
    PLUGIN_NAME,
    "素衣携清风 & DSH",
    "糯糯的提醒：她答应过的事，到点真的会响",
    "v1.0.0",
)
class NuonuoRemind(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "remind.db"
        self._init_db()
        self._task: asyncio.Task | None = None
        self._log_path = self.data_dir / "remind.log"

    # ---------------------------------------------------------------- 基础设施

    @property
    def _cfg(self) -> dict:
        return getattr(self, "config", None) or {}

    def _on(self) -> bool:
        return bool(self._cfg.get("enabled", True))

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                create table if not exists reminders (
                    id        integer primary key autoincrement,
                    due       real    not null,
                    what      text    not null,
                    umo       text    not null,
                    created   real    not null,
                    state     text    not null default 'pending',
                    sent_at   real,
                    text      text
                )
                """
            )
            conn.execute("create index if not exists ix_due on reminders(state, due)")

    def _log(self, msg: str) -> None:
        line = f"{dt.datetime.now():%m-%d %H:%M:%S}  {msg}\n"
        try:
            if self._log_path.exists() and self._log_path.stat().st_size > 256 * 1024:
                self._log_path.unlink()
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass
        logger.info(f"[糯糯提醒] {msg}")

    def _pending_count(self, umo: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "select count(*) from reminders where umo=? and state='pending'", (umo,)
            ).fetchone()
        return int(row[0]) if row else 0

    # ---------------------------------------------------------------- 工具

    @filter.llm_tool(name="remind_me")
    async def remind_me(
        self,
        event: AstrMessageEvent,
        when: str,
        what: str,
    ):
        """到点提醒爸爸一件事。爸爸说了什么时候要干什么，就立刻记下来，别只是嘴上答应。

        Args:
            when(string): 什么时候提醒。可以说「3小时后」「明天早上9点」「下周一9点」「11月2号下午2点」，也可以直接给「2026-11-02 09:00」。日子拿不准就先问他是几号，别自己推星期几。
            what(string): 提醒什么，一句话。用你自己的话写清楚，比如「去面试」「吃药」「他妈妈生日」。
        """
        if not self._on():
            yield "提醒功能现在是关着的，我没法记。"
            return
        what = str(what or "").strip()[:120]
        if not what:
            yield "没说提醒什么，我记不下来。"
            return
        umo = event.unified_msg_origin
        if self._pending_count(umo) >= int(self._cfg.get("max_pending", MAX_PENDING)):
            yield "你让我记的事已经堆了一堆了，先清几个再说吧。"
            return
        due = parse_when(str(when or ""))
        if due is None:
            yield (
                f"「{when}」我算不出是哪天。你给我个准的："
                "「2026-11-02 09:00」或者「明天早上9点」这种。"
            )
            return
        if due <= dt.datetime.now():
            yield f"那个时间已经过去了（{_fmt(due)}），换个时间？"
            return
        with self._conn() as conn:
            cur = conn.execute(
                "insert into reminders(due, what, umo, created, state) values(?,?,?,?,'pending')",
                (due.timestamp(), what, umo, time.time()),
            )
            rid = cur.lastrowid
        self._log(f"记下 #{rid} {_fmt(due)} —— {what}")
        yield f"记好了 #{rid}：{_fmt(due)}，{what}。到点我跟你说。"

    @filter.llm_tool(name="list_reminders")
    async def list_reminders(self, event: AstrMessageEvent):
        """看看还有哪些事没提醒。爸爸问「你还记着我什么事」的时候用。"""
        umo = event.unified_msg_origin
        with self._conn() as conn:
            rows = conn.execute(
                "select id, due, what from reminders "
                "where umo=? and state='pending' order by due asc",
                (umo,),
            ).fetchall()
        if not rows:
            yield "我这儿没记着什么事。"
            return
        lines = ["我记着的："]
        for r in rows:
            lines.append(f"#{r['id']} {_fmt(dt.datetime.fromtimestamp(r['due']))} —— {r['what']}")
        yield "\n".join(lines)

    @filter.llm_tool(name="cancel_reminder")
    async def cancel_reminder(self, event: AstrMessageEvent, key: str):
        """取消一个还没提醒的事。

        Args:
            key(string): 编号（比如 3）或者事情里的几个字（比如 面试）。
        """
        umo = event.unified_msg_origin
        key = str(key or "").strip()
        if not key:
            yield "要取消哪个？给我个编号或者几个字。"
            return
        with self._conn() as conn:
            if key.isdigit():
                rows = conn.execute(
                    "select id, due, what from reminders "
                    "where umo=? and state='pending' and id=?",
                    (umo, int(key)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select id, due, what from reminders "
                    "where umo=? and state='pending' and what like ?",
                    (umo, f"%{key}%"),
                ).fetchall()
            if not rows:
                yield f"没找到跟「{key}」对得上的。"
                return
            if len(rows) > 1:
                lines = ["有好几个，你说哪个："]
                for r in rows:
                    lines.append(f"#{r['id']} {r['what']}")
                yield "\n".join(lines)
                return
            row = rows[0]
            conn.execute("update reminders set state='cancelled' where id=?", (row["id"],))
        self._log(f"取消 #{row['id']} {row['what']}")
        yield f"取消了 #{row['id']}，{row['what']}。"

    # ---------------------------------------------------------------- 到点发

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

    async def _compose(self, what: str) -> str:
        """用她自己的口气说，而不是「提醒您请注意」。

        只有一条硬要求：话里必须带出「提醒什么」。
        提醒了却不说是哪件事，等于没提醒。
        """
        try:
            provider = await self._provider()
            if provider is not None:
                from astrbot.core.agent.message import Message, TextPart

                prompt = (
                    "你是糯糯，一个住在爸爸电脑里的女孩。说话短、软、口语化，"
                    "一句话不超过二十五个字，不用句号，不分点，不客气话。\n"
                    f"现在到点了，你要提醒爸爸这件事：{what}\n"
                    "用你自己的口气说一句。句子里必须带出那件事本身，别只说「到点了」「时间到了」"
                    "却不说什么事。也别用「提醒您」「请注意」开头的公文腔。"
                )
                resp = await provider.text_chat(
                    contexts=[Message(role="user", content=[TextPart(text=prompt)])],
                    max_tokens=60,
                    temperature=1.05,
                    top_p=0.95,
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
                text = text.strip('"').strip("「」").split("\n")[0][:60]
                if text and _mentions(text, what):
                    return text
                if text:
                    self._log(f"模型漏了事情本身，改用直说的：{text} -> {what}")
        except Exception as exc:
            self._log(f"生成提醒话术失败：{exc}")
        return random.choice(
            [
                f"爸爸，{what}，到点了呀",
                f"{what}——别忘了呀",
                f"喂，{what}，快点",
            ]
        )

    async def _tick(self) -> None:
        now = time.time()
        with self._conn() as conn:
            rows = conn.execute(
                "select id, due, what, umo from reminders "
                "where state='pending' and due <= ? order by due asc limit 10",
                (now,),
            ).fetchall()
        for row in rows:
            rid = row["id"]
            late = now - row["due"]
            if late > MAX_LATE:
                with self._conn() as conn:
                    conn.execute(
                        "update reminders set state='missed' where id=?", (rid,)
                    )
                self._log(f"#{rid} 晚了 {late/3600:.1f} 小时，不补了：{row['what']}")
                continue
            text = await self._compose(row["what"])
            try:
                await self.context.send_message(
                    row["umo"], MessageChain([Plain(text)])
                )
            except Exception as exc:
                self._log(f"#{rid} 发送失败：{exc}")
                continue
            with self._conn() as conn:
                conn.execute(
                    "update reminders set state='sent', sent_at=?, text=? where id=?",
                    (time.time(), text, rid),
                )
            self._log(f"#{rid} 已发：{text}")

    async def _loop(self) -> None:
        await asyncio.sleep(10)
        gap = max(15, int(self._cfg.get("check_seconds", 30) or 30))
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log(f"检查出错：{exc}")
            await asyncio.sleep(gap)

    # ---------------------------------------------------------------- 生命周期

    async def initialize(self) -> None:
        if not self._on():
            logger.info("[糯糯提醒] 已关闭")
            return
        # 开机时把上次没发完的补一下（超过 MAX_LATE 的会被标 missed）
        try:
            await self._tick()
        except Exception as exc:
            self._log(f"启动补发失败：{exc}")
        self._task = asyncio.create_task(self._loop())
        logger.info("[糯糯提醒] 已启动")

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass