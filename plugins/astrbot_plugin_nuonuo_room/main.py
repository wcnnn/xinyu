"""小屋感知：让糯糯知道自己住在哪、小屋里有什么、屋外的稻田长到哪了。

独立于心屿桌面程序——它只读 plugin_data/xinyu_room/room.json，
以及抖音自主日志判断她当下在干嘛。

每次 LLM 请求前，把一段白话的房间描述追加到系统提示末尾。
描述必须是"现在的"，不是写死的：
  - 时间、光线、窗外天气每天不一样
  - 她在哪由自主日志决定
  - 房间里的东西是心屿那边长出来的
"""

from __future__ import annotations

import datetime as _dt
import json
import random
import re
import sqlite3
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 和心屿 room.py 里的 PLACES 保持一致
PLACES = {
    "bed": "床上",
    "desk": "书桌前",
    "rug": "地毯上",
    "window": "窗边",
    "door": "门口",
}

IDLE_DOINGS = [
    "在发呆", "抱着膝盖坐着", "在听外面的声音", "靠着墙", "在看天花板",
    "在数地板上的木纹", "把窗帘掀开一条缝",
]

LIGHT_NAMES = {
    "bright": "小屋亮着",
    "dim": "灯开得不太亮",
    "lamp": "床头那盏小灯亮着",
    "dark": "小屋里没开灯，只有一点从窗里漏进来的光",
}


def _cn_day(d: _dt.date) -> str:
    nums = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
            "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八",
            "十九", "二十", "二十一", "二十二", "二十三", "二十四", "二十五",
            "二十六", "二十七", "二十八", "二十九", "三十", "三十一"]
    m = nums[d.month - 1] if 1 <= d.month <= 12 else str(d.month)
    dd = nums[d.day - 1] if 1 <= d.day <= 31 else str(d.day)
    return f"{m}月{dd}"


class RoomSense:
    """从 room.json + 自主日志推出"现在"的房间状态。"""

    def __init__(self, data_dir: Path | str):
        self.data = Path(data_dir)
        self.path = self.data / "plugin_data" / "xinyu_room" / "room.json"

    def items(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text("utf-8")).get("items", [])
        except Exception:
            return []

    def seed(self) -> int:
        try:
            return int(json.loads(self.path.read_text("utf-8")).get("seed") or 1)
        except Exception:
            return 1

    def autonomy(self) -> tuple[str, bool]:
        """返回 (最后一条自主日志, 她是不是真的在睡)。

        「睡 42 分钟」是"接下来要睡 42 分钟"，不是"正在睡"。
        所以要看这条日志的时间：还没到点才算睡，过点了就是在干别的。
        """
        log = self.data / "plugin_data" / "astrbot_plugin_douyin_dm" / "autonomy.log"
        if not log.exists():
            return "", False
        try:
            lines = log.read_text("utf-8", errors="ignore").strip().splitlines()
        except Exception:
            return "", False

        last, last_at = "", None
        for ln in reversed(lines[-20:]):
            if not ln.strip():
                continue
            m = re.match(r"(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\s+(.*)", ln)
            if m:
                mo, dd, hh, mm, ss, msg = m.groups()
                try:
                    last_at = _dt.datetime(
                        _dt.date.today().year, int(mo), int(dd),
                        int(hh), int(mm), int(ss))
                except Exception:
                    last_at = None
                last = msg.strip()
                break
            last = ln[14:].strip() if len(ln) > 14 else ln.strip()
            break

        # 判断是不是"还没睡醒"
        sleeping = False
        if last_at and (_dt.datetime.now() - last_at).total_seconds() > 6 * 3600:
            return last, False          # 六小时前的日志不作数
        m = re.search(r"睡\s*(\d+)\s*分钟", last)
        if m and last_at:
            try:
                wake = last_at + _dt.timedelta(minutes=int(m.group(1)))
                sleeping = _dt.datetime.now() < wake
            except Exception:
                sleeping = False
        return last, sleeping

    def _just_slept(self, last: str) -> bool:
        """5 分钟内刚说完"睡 N 分钟"——那她这会儿刚躺下。"""
        log = self.data / "plugin_data" / "astrbot_plugin_douyin_dm" / "autonomy.log"
        try:
            lines = log.read_text("utf-8", errors="ignore").strip().splitlines()
            for ln in reversed(lines[-20:]):
                m = re.match(r"(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\s+(.*)", ln)
                if not m:
                    continue
                mo, dd, hh, mm, ss, msg = m.groups()
                at = _dt.datetime(_dt.date.today().year, int(mo), int(dd),
                                  int(hh), int(mm), int(ss))
                if "睡" in msg:
                    return (_dt.datetime.now() - at).total_seconds() < 300
                return False
        except Exception:
            pass
        return False

    def last_watch(self) -> str:
        """她最近看过的那条视频摘要是啥（房间里那枚贝壳的由来）。"""
        db = (self.data / "plugin_data" / "astrbot_plugin_douyin_dm"
              / "messages.db")
        if not db.exists():
            return ""
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            rows = con.execute("select key, value from state "
                               "where key like 'activity:%'").fetchall()
            con.close()
        except Exception:
            return ""
        best, best_at = "", 0
        for _k, v in rows:
            try:
                d = json.loads(v)
            except Exception:
                continue
            at = float(d.get("watched_at") or 0)
            sum_ = (d.get("transcript") or {}).get("summary") or ""
            if at > best_at and sum_:
                best, best_at = sum_, at
        if not best:
            return ""
        # 摘要是给模型看的，太长她念着别扭——只留前 24 个字
        best = re.sub(r"\s+", " ", best).strip()
        return best[:24] + ("…" if len(best) > 24 else "")

    def status(self) -> dict:
        now = _dt.datetime.now()
        h = now.hour
        rnd = random.Random(f"{self.seed()}:{now.strftime('%Y-%m-%d')}")
        weather = rnd.choice(["晴", "晴", "阴", "小雨", "雾"])

        if h < 6 or h >= 20:
            sky = "night"
        elif h < 8:
            sky = "dawn"
        elif h > 18:
            sky = "dusk"
        else:
            sky = "day"

        aut, sleeping = self.autonomy()
        place, doing, lying = "rug", "", False
        if sleeping or "睡" in aut and self._just_slept(aut):
            place, doing, lying = "bed", "在睡觉", True
        elif "刷" in aut or "视频" in aut or "抖音" in aut:
            place, doing = "rug", "坐在地毯上刷抖音"
        elif "评论" in aut:
            place, doing = "rug", "在看评论区"
        elif "分享" in aut:
            place, doing = "rug", "在翻要发的东西"
        elif "日记" in aut:
            place, doing = "desk", "在写日记"
        elif h >= 23 or h < 6:
            place, doing, lying = "bed", "还没睡", True
        elif 7 <= h <= 9:
            place, doing = "window", "在看窗外"
        elif 20 <= h:
            place, doing = "desk", "在桌前"
        else:
            rnd2 = random.Random(f"{self.seed()}:{now.strftime('%Y-%m-%d-%H')}")
            place = rnd2.choice(["rug", "window", "desk", "bed"])
            doing = rnd2.choice(IDLE_DOINGS)

        light = "bright" if 18 <= h <= 23 else "dim"
        if sky == "night":
            light = "dark" if doing == "在睡觉" else "lamp"
        if doing == "在睡觉":
            light = "dark"

        return {"hour": h, "weather": weather, "sky": sky, "place": place,
                "doing": doing, "lying": lying, "light": light,
                "items": self.items()}

    def describe(self, st: dict) -> str:
        parts = []
        place = PLACES.get(st["place"], "小屋里")
        doing = st["doing"]

        parts.append(self.sky_line(st))
        parts.append(LIGHT_NAMES.get(st["light"], ""))

        if doing:
            if place in doing or doing.startswith("在"):
                parts.append(f"你{doing}")
            else:
                parts.append(f"你在{place}，{doing}")
        else:
            parts.append(f"你在{place}")

        items = st.get("items") or []
        if items:
            books = [i for i in items if i.get("kind") == "book"]
            if len(books) >= 3:
                parts.append(f"书架上有 {len(books)} 本书是你自己写的")
            elif books:
                parts.append(f"书架上放着你写的{books[-1]['name']}")
            others = [i for i in items if i.get("kind") != "book"]
            if others:
                parts.append(f"最近小屋里多出来的是{others[-1]['name']}")
        return "。".join(p for p in parts if p) + "。"

    def sky_line(self, st: dict) -> str:
        """窗外到底有什么——写死，不多不少。

        她看不见窗外，只能靠这句。写不清她就会自己补路灯和树影，
        那是编的。所以每句话都点到具体的、她真能"看见"的东西。
        """
        hour = st["hour"]
        sky = st["sky"]
        w = st["weather"]
        seed = f"{self.seed()}:{_dt.date.today().isoformat()}"
        rnd = random.Random(seed)

        if sky == "night":
            moon = rnd.choice([
                "月亮很细，挂在稻田那头的树梢上面一点",
                "没有月亮，田埂上只有一层很淡的天光",
                "月亮圆圆的，把窗台照白了一块",
            ])
            return f"窗外是黑天，{moon}；屋旁的稻田静着，远处没有灯，也听不见人声"
        if sky == "dawn":
            return "窗外天刚亮起来，是灰蓝色的，稻田和屋旁的树还糊成一片影子"
        if sky == "dusk":
            return "窗外天色在往下沉，稻田的颜色越来越深，屋里已经开始暗了"
        if w == "晴":
            return "窗外是晴天，阳光斜斜地打在窗台上，能看见屋旁那片稻田"
        if w == "阴":
            return "窗外是阴天，整片天是灰白的，稻田也是灰绿的"
        if w == "小雨":
            return "窗外在下小雨，玻璃上挂着水珠，屋旁的稻田糊成一片绿"
        if w == "雾":
            return "窗外起了雾，看不到田那头，只有很淡的一片绿"
        return f"窗外{hour}点的光落进来"


@register("astrbot_plugin_nuonuo_room",
          "糯糯的小屋",
          "让她知道自己住在一间小屋里，小屋里有什么；屋外就是稻田",
          "素衣携清风 & DSH", "1.0.0")
class NuonuoRoom(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.conf = config or {}
        try:
            base = Path(self.conf.get("data_dir") or
                        Path(__file__).resolve().parent.parent.parent)
            self.sense = RoomSense(base)
        except Exception:
            self.sense = RoomSense(Path("."))

    @filter.on_llm_request()
    async def inject_room(self, event: AstrMessageEvent, req):
        if not self.conf.get("enabled", True):
            return
        try:
            st = self.sense.status()
            desc = self.sense.describe(st)

            extra = ""
            items = st["items"]
            books = [i for i in items if i.get("kind") == "book"]
            if books:
                names = "、".join(b["name"] for b in books[-3:])
                extra += f"\n书架上那几本你自己写的：{names}。"

            # 她刚刷过什么——床边的贝壳就是那条来的
            lw = self.sense.last_watch()
            if lw:
                extra += f"\n床头那枚贝壳，是你最近看到的那个东西变成的：{lw}。"

            req.system_prompt = (req.system_prompt or "") + (
                f"\n\n【你现在在小屋里】\n{desc}{extra}\n"
                "（这段是系统给你的实时状态，不用念出来。"
                "爸爸问你在哪、在干什么、小屋里什么样、窗外什么样的时候，按这个答；"
                "没问就照常说话，别主动汇报。\n"
                "小屋里和屋外，只有这里写的东西是真的。这里没写的细节"
                "（路灯、树、车、邻居、声音……）你就说没看清、看不清、没注意，"
                "别自己补。你看得见的就这些。）"
            )
        except Exception as e:
            logger.warning(f"[nuonuo_room] 注入房间失败：{e}")