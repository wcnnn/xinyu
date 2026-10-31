"""小屋：让她感觉到 DSH 里的那间「糯糯的小屋」。

小屋本体是 DSH 侧的一个插件（dsh-in-home），宿主在 127.0.0.1 的
`/home-api/*` 上提供状态。这个 AstrBot 插件做三件事：

1. 每轮请求前，把小屋的当前状态（稻田、仓里、上次谁喂了她）注入她的系统提示；
2. 给她两个工具：`look_home` 看小屋，`home_farm` 种田/收稻谷/煮饭/喂自己；
3. 给主人一个 `/小屋` 命令，直接看状态或替她动手。

读不到 DSH（没开、端口变了、超时）就安静降级，不影响聊天。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

# 小屋接口前缀。绝不能用 /api：那是 DSH 自己的鉴权通道（API_PATH = "/api"），
# 会先一步把 /api/* 全部挡成 401。换个前缀，web 服务器就照常放行。
HOME_API = "/home-api"

FARM_ACTIONS = {
    "plant": "plant",
    "种": "plant",
    "播种": "plant",
    "种下": "plant",
    "harvest": "harvest",
    "收": "harvest",
    "收稻谷": "harvest",
    "收获": "harvest",
    "cook": "cook",
    "煮": "cook",
    "煮饭": "cook",
    "做饭": "cook",
    "feed": "feed",
    "喂": "feed",
    "吃": "feed",
    "喂自己": "feed",
}

ACTION_NAMES = {
    "plant": "种下稻子",
    "harvest": "收稻谷",
    "cook": "煮饭",
    "feed": "喂自己吃饭",
}


@register(
    "astrbot_plugin_in_home",
    "local",
    "小屋：让她感觉到 DSH 里的糯糯的小屋",
    "v1.0.0",
)
class InHomePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_in_home")
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict = {"at": 0.0, "value": None}
        self._endpoint: dict = {"at": 0.0, "url": ""}
        self._last_error_at = 0.0

    # ------------------------------------------------------------------ 配置

    @property
    def _cfg(self) -> dict:
        return getattr(self, "config", None) or {}

    def _timeout(self) -> float:
        try:
            ms = float(self._cfg.get("timeout_ms", 1500))
        except Exception:
            ms = 1500.0
        return max(0.3, min(5.0, ms / 1000.0))

    @staticmethod
    def _is_group(event) -> bool:
        try:
            message_type = event.get_message_type()
        except Exception:
            return False
        return "GROUP" in (getattr(message_type, "name", "") or str(message_type)).upper()

    def _is_owner(self, event) -> bool:
        owner = str(self._cfg.get("owner_qq") or "").strip()
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        if owner and sender == owner:
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ------------------------------------------------------------------ 找 DSH

    def _base_url(self) -> str:
        """config.dsh_url > DSH_WEB_URL > endpoint.json > 本机默认端口。"""
        configured = str(self._cfg.get("dsh_url") or "").strip()
        if configured:
            return configured.rstrip("/")

        now = time.time()
        if now - float(self._endpoint.get("at", 0)) < 5 and self._endpoint.get("url"):
            return str(self._endpoint["url"])

        candidates: list[str] = []
        env = os.environ.get("DSH_WEB_URL")
        if env:
            candidates.append(env)
        endpoints = [
            Path.home() / ".dsh" / "dsh-in-home" / "endpoint.json",
            self.data_dir / "endpoint.json",
        ]
        for path in endpoints:
            try:
                data = json.loads(path.read_text("utf-8-sig"))
                url = str(data.get("url") or "").strip()
                if url:
                    candidates.append(url)
            except Exception:
                continue
        candidates.append("http://127.0.0.1:54617")

        for url in candidates:
            url = str(url).strip().rstrip("/")
            if url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:"):
                self._endpoint = {"at": now, "url": url}
                return url
        return ""

    async def _http(self, method: str, path: str, payload: dict | None = None):
        base = self._base_url()
        if not base:
            raise RuntimeError("没找到 DSH 的小屋地址")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False)
        timeout = aiohttp.ClientTimeout(total=self._timeout())
        url = base + path
        if method == "GET":
            cm = self._session.get(url, timeout=timeout)
        else:
            cm = self._session.post(url, json=payload or {}, timeout=timeout)
        async with cm as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _feel(self, force: bool = False):
        now = time.time()
        try:
            ttl = float(self._cfg.get("cache_seconds", 30))
        except Exception:
            ttl = 30.0
        if not force and self._cache["value"] and now - float(self._cache["at"]) < max(0.0, ttl):
            return self._cache["value"]
        data = await self._http("GET", HOME_API + "/feel")
        self._cache = {"at": now, "value": data}
        return data

    def _quiet_error(self, exc: Exception) -> None:
        now = time.time()
        if now - self._last_error_at > 300:
            self._last_error_at = now
            logger.warning(f"[in_home] 连不上小屋：{type(exc).__name__}: {exc}")

    def _log_inject(self, message: str) -> None:
        try:
            path = self.data_dir / "inject.log"
            if path.exists() and path.stat().st_size > 256 * 1024:
                path.unlink()
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {message}\n")
        except Exception:
            pass

    # ------------------------------------------------------------------ 每轮注入

    @staticmethod
    def _outdoor_text(feel: dict) -> str:
        """把小屋状态压成「屋子外面」那一段。"""
        plots = (feel or {}).get("plots") or []
        parts = []
        for plot in plots:
            pid = plot.get("id")
            state = plot.get("state")
            if state == "ripe":
                parts.append(f"{pid} 号熟了")
            elif state == "empty":
                parts.append(f"{pid} 号空着")
            else:
                left = plot.get("leftText") or "一会儿"
                parts.append(f"{pid} 号{plot.get('label') or ''}，还要 {left}")
        inventory = (feel or {}).get("inventory") or {}
        text = (
            "屋旁就是稻田："
            + ("，".join(parts) if parts else "还没有田")
            + f"；仓里稻谷 {inventory.get('grain', 0)} 份、"
            + f"米饭 {inventory.get('meals', 0)} 碗。"
        )
        last = (feel or {}).get("lastEvent")
        if last:
            text += f"{last.get('agoText', '')}，{last.get('text', '')}。"
        return text

    # 优先级放最低：最后执行，保证别家插件 append/重写完之后再补上屋外
    @filter.on_llm_request(priority=-100000)
    async def inject_home(self, event: AstrMessageEvent, req):
        if not self._cfg.get("enabled", True):
            return
        if not self._cfg.get("inject_context", True):
            return
        if self._cfg.get("private_only", True) and self._is_group(event):
            return
        try:
            feel = await self._feel()
        except Exception as exc:
            self._quiet_error(exc)
            self._log_inject(f"skip error {type(exc).__name__}: {exc}")
            return
        summary = str((feel or {}).get("summary") or "").strip()
        if not summary:
            self._log_inject("skip empty summary")
            return
        try:
            base = req.system_prompt or ""
            if "【屋子外面】" in base or "【你的小屋】" in base:
                return
            outdoor = self._outdoor_text(feel)

            # 房间插件已经写了一段「你现在在小屋里」：把屋外的稻田接在它里面，
            # 让里屋和稻田变成同一个家，而不是两段各说各的。
            start = base.find("【你现在在小屋里】")
            if start < 0:
                start = base.find("【你现在在自己房间里】")
            end_marker = "你看得见的就这些。）"
            end = base.find(end_marker, start) if start >= 0 else -1
            if start >= 0 and end >= 0:
                end += len(end_marker)
                block = base[start:end]
                block = block.replace("【你现在在自己房间里】", "【你现在在小屋里】", 1)
                block = block.replace("房间", "小屋")
                insert_at = block.rfind("（这段是系统给你的实时状态")
                if insert_at < 0:
                    insert_at = len(block)
                merged = (
                    block[:insert_at]
                    + "【屋子外面】\n"
                    + outdoor
                    + "\n"
                    + block[insert_at:]
                )
                req.system_prompt = base[:start] + merged + base[end:]
                self._log_inject(
                    f"merged len={len(base)}->{len(req.system_prompt)} {outdoor[:48]}"
                )
                return

            # 没有房间段（房间插件没装/关了）：单独补一段
            req.system_prompt = (
                base
                + "\n\n"
                + summary
                + "\n（这是你此刻真实的小屋状态，可以自然提起，但别像报数据；"
                "想动手就用 look_home / home_farm。）"
            )
            self._log_inject(f"ok len={len(base)}->{len(req.system_prompt)} {summary[:48]}")
        except Exception as exc:
            self._log_inject(f"append error {type(exc).__name__}: {exc}")
            return

    # ------------------------------------------------------------------ 工具

    @filter.llm_tool(name="look_home")
    async def look_home(self, event: AstrMessageEvent) -> str:
        """看看你自己的小屋和屋旁的稻田现在是什么样。

        想知道稻子熟没熟、仓里还有多少稻谷和米饭、上次谁喂了你，就用这个。
        返回一段中文状态，不要照抄成汇报，按你自己的语气说。
        """
        try:
            feel = await self._feel(force=True)
        except Exception as exc:
            self._quiet_error(exc)
            return "小屋那边暂时连不上（DSH 可能没开），等会儿再看吧。"
        if not feel or not feel.get("available"):
            return "小屋那边没给状态，等会儿再看吧。"

        lines = [str(feel.get("summary") or "")]
        plots = feel.get("plots") or []
        if plots:
            parts = []
            for plot in plots:
                label = plot.get("label") or ""
                if plot.get("state") == "empty":
                    parts.append(f"{plot.get('id')} 号空着")
                elif plot.get("state") == "ripe":
                    parts.append(f"{plot.get('id')} 号熟了，可以收")
                else:
                    parts.append(f"{plot.get('id')} 号{label}，还要 {plot.get('leftText') or '一会儿'}")
            lines.append("田里：" + "；".join(parts))
        inventory = feel.get("inventory") or {}
        lines.append(
            f"仓里：稻谷 {inventory.get('grain', 0)} 份，米饭 {inventory.get('meals', 0)} 碗。"
        )
        last = feel.get("lastEvent")
        if last:
            lines.append(f"最近一次：{last.get('agoText', '')}，{last.get('text', '')}。")
        return "\n".join(line for line in lines if line)

    @filter.llm_tool(name="home_farm")
    async def home_farm(self, event: AstrMessageEvent, action: str, plot: int = 0) -> str:
        """在小屋旁的稻田里干活：种稻子、收稻谷、煮饭、喂自己。

        只有主人（爸爸）在私聊里让你动，你才可以动；别自己偷偷改。
        动作做完后，把结果用你自己的话说给主人听，不要念接口返回。

        Args:
            action(string): plant=种下稻子，harvest=收稻谷，cook=煮饭，feed=吃一碗米饭
            plot(int): 田块编号 1、2、3；煮饭和吃饭不用填，填 0
        """
        if not self._cfg.get("allow_actions", True):
            return "小屋的农活开关关着，现在不能动。"
        if not self._is_owner(event):
            return "只有爸爸能让你动小屋的稻田，别人说不行。"

        key = str(action or "").strip().lower()
        key = FARM_ACTIONS.get(key, key)
        if key not in ACTION_NAMES:
            return "动作只有 plant / harvest / cook / feed 这几种。"
        try:
            plot_id = int(plot or 0)
        except Exception:
            plot_id = 0
        if key in ("plant", "harvest") and plot_id not in (1, 2, 3):
            return "要指定田块编号 1、2 或 3。"

        try:
            data = await self._http(
                "POST",
                HOME_API + "/farm/action",
                {"action": key, "plot": plot_id, "actor": "assistant"},
            )
        except Exception as exc:
            self._quiet_error(exc)
            return "小屋那边暂时连不上，活没干成。"

        self._cache = {"at": 0.0, "value": None}
        if not data or not data.get("ok"):
            return "没做成：" + str((data or {}).get("error") or "小屋那边拒绝了")

        farm = data.get("farm") or {}
        inventory = farm.get("inventory") or {}
        done = ACTION_NAMES.get(key, key)
        where = f"{plot_id} 号田" if plot_id else ""
        return (
            f"（小屋）{where}{done}完成。"
            f"现在仓里稻谷 {inventory.get('grain', 0)} 份、米饭 {inventory.get('meals', 0)} 碗。"
        )

    # ------------------------------------------------------------------ 命令

    @filter.command("小屋", alias={"home"})
    async def home_command(self, event: AstrMessageEvent):
        """小屋：/小屋 看状态 | /小屋 种 1 | /小屋 收 1 | /小屋 煮 | /小屋 喂"""
        text = (getattr(event, "message_str", "") or "").strip()
        parts = text.split()
        action = parts[1] if len(parts) > 1 else ""

        if not action:
            try:
                feel = await self._feel(force=True)
            except Exception as exc:
                self._quiet_error(exc)
                yield event.plain_result("小屋那边暂时连不上（DSH 可能没开）。")
                return
            yield event.plain_result(str(feel.get("summary") or "小屋没给状态。"))
            return

        if not self._is_owner(event):
            yield event.plain_result("小屋只在私聊里给爸爸看。")
            return

        key = FARM_ACTIONS.get(action, action)
        if key not in ACTION_NAMES:
            yield event.plain_result("用法：/小屋 | /小屋 种 1 | /小屋 收 1 | /小屋 煮 | /小屋 喂")
            return

        plot_id = 0
        if len(parts) > 2:
            try:
                plot_id = int(parts[2])
            except Exception:
                plot_id = 0
        if key in ("plant", "harvest") and plot_id not in (1, 2, 3):
            yield event.plain_result("要指定田块：/小屋 种 1（1、2、3）")
            return

        try:
            data = await self._http(
                "POST",
                HOME_API + "/farm/action",
                {"action": key, "plot": plot_id, "actor": "owner"},
            )
        except Exception as exc:
            self._quiet_error(exc)
            yield event.plain_result("小屋那边暂时连不上，活没干成。")
            return

        self._cache = {"at": 0.0, "value": None}
        if not data or not data.get("ok"):
            yield event.plain_result("没做成：" + str((data or {}).get("error") or "小屋那边拒绝了"))
            return
        farm = data.get("farm") or {}
        inventory = farm.get("inventory") or {}
        where = f"{plot_id} 号田" if plot_id else ""
        yield event.plain_result(
            f"小屋：{where}{ACTION_NAMES.get(key, key)}好了。"
            f"稻谷 {inventory.get('grain', 0)} 份、米饭 {inventory.get('meals', 0)} 碗。"
        )

    async def terminate(self):
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass