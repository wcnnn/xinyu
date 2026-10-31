"""电脑手脚：截图给她看、鼠标点、键盘敲、开程序。

安全边界：
- 只在私聊生效（群聊直接拒绝）
- 每分钟操作次数上限
- 打开程序默认关闭，开启后也只允许白名单
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from . import desktop_io as dio
except Exception:  # 兜底：万一不是以包的方式加载
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    import desktop_io as dio

ALLOWED_APPS = {
    "notepad": "notepad.exe",
    "记事本": "notepad.exe",
    "calc": "calc.exe",
    "计算器": "calc.exe",
    "mspaint": "mspaint.exe",
    "画图": "mspaint.exe",
    "explorer": "explorer.exe",
    "文件": "explorer.exe",
    "edge": "msedge.exe",
    "browser": "msedge.exe",
    "浏览器": "msedge.exe",
    "taskmgr": "taskmgr.exe",
}


@register(
    "astrbot_plugin_pc_hands",
    "local",
    "电脑手脚：截图、鼠标、键盘、开程序",
    "v1.0.0",
)
class PcHandsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_pc_hands")
        self.shot_dir = self.data_dir / "shots"
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self._pending: str = ""
        self._actions: list[float] = []
        self._unlock_until: float = 0.0

    # ------------------------------------------------------------------ 公共

    @property
    def _cfg(self) -> dict:
        return getattr(self, "config", None) or {}

    @staticmethod
    def _is_group(event) -> bool:
        try:
            mt = event.get_message_type()
        except Exception:
            return False
        return "GROUP" in (getattr(mt, "name", "") or str(mt)).upper()

    def _blocked(self, event) -> bool:
        if not self._cfg.get("enabled", True):
            return True
        return bool(self._cfg.get("private_only", True)) and self._is_group(event)

    def _rate_ok(self) -> bool:
        limit = int(self._cfg.get("max_actions_per_minute", 40) or 40)
        now = time.time()
        self._actions = [t for t in self._actions if now - t < 60]
        if len(self._actions) >= limit:
            return False
        self._actions.append(now)
        return True

    # ------------------------------------------------------------------ 修改授权

    def _ask_granted(self) -> bool:
        """读「动电脑前先问」插件的批准状态。"""
        try:
            import json as _json
            from pathlib import Path as _P

            p = (_P(__file__).resolve().parents[2] / "plugin_data"
                 / "astrbot_plugin_nuonuo_ask" / "state.json")
            if not p.exists():
                return False
            d = _json.loads(p.read_text("utf-8"))
            return time.time() < float(d.get("granted_until") or 0)
        except Exception:
            return False

    def _may_modify(self) -> bool:
        """要先问一声，批准了才能动。

        - nuonuo_ask 里点了「同意」→ 允许（限时）
        - 或主人发了 /允许修改 → 允许（限时）
        - modify_requires_approval 设成 false 则完全放开
        """
        if not self._cfg.get("modify_requires_approval", True):
            return True
        if self._ask_granted():
            return True
        return time.time() < self._unlock_until

    def _lock_hint(self) -> str:
        left = int(self._unlock_until - time.time())
        if left > 0:
            return f"现在不是授权状态（剩余 {left} 秒）。"
        return "我得先问爸爸一声。你要我先说想干嘛吗？"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("允许修改")
    async def grant_modify(self, event: AstrMessageEvent):
        """允许修改：/允许修改 [分钟]，默认 10 分钟，最长 120 分钟。"""
        parts = (getattr(event, "message_str", "") or "").strip().split()
        minutes = 10
        if len(parts) > 1:
            try:
                minutes = max(1, min(120, int(parts[1])))
            except ValueError:
                minutes = 10
        self._unlock_until = time.time() + minutes * 60
        yield event.plain_result(f"好，接下来 {minutes} 分钟可以动你的电脑。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("收回权限")
    async def revoke_modify(self, event: AstrMessageEvent):
        """收回权限：立刻回到只读。"""
        self._unlock_until = 0.0
        yield event.plain_result("权限收回了，现在只有使用权。")

    # ------------------------------------------------------------------ 工具


    @filter.llm_tool(name="pc_look")
    async def pc_look(self, event: AstrMessageEvent):
        """看你的电脑屏幕（截图）。想知道你现在在干什么、屏幕长什么样时调用。"""
        if self._blocked(event):
            yield "这里不能看屏幕。"
            return
        if not self._rate_ok():
            yield "操作太频繁了，慢一点。"
            return
        try:
            target = self.shot_dir / "latest.jpg"
            size = await asyncio.to_thread(dio.screenshot, str(target))
            self._pending = str(target)
            yield f"看过屏幕了，分辨率 {size[0]}x{size[1]}。"
        except Exception as exc:
            logger.warning(f"[电脑手脚] 截图失败：{exc}")
            yield f"截图失败：{exc}"

    @filter.llm_tool(name="pc_send_screen")
    async def pc_send_screen(self, event: AstrMessageEvent):
        """把屏幕截图直接发给他，让他能在聊天里看到图片。他要求"截图给我/发我看看"时用这个。"""
        if self._blocked(event):
            yield "这里不能发截图。"
            return
        if not self._rate_ok():
            yield "操作太频繁了，慢一点。"
            return
        try:
            target = self.shot_dir / "send.jpg"
            await asyncio.to_thread(dio.screenshot, str(target))
            yield event.image_result(str(target))
        except Exception as exc:
            logger.warning(f"[电脑手脚] 发送截图失败：{exc}")
            yield f"截图失败：{exc}"

    @filter.llm_tool(name="pc_click")
    async def pc_click(self, event: AstrMessageEvent, x: str, y: str, button: str = "left", double: str = "false"):
        """在屏幕坐标 (x, y) 点一下鼠标。

        Args:
            x(string): 横坐标像素
            y(string): 纵坐标像素
            button(string): left 或 right，默认 left
            double(string): 是否双击，填 true 或 false
        """
        if not self._may_modify():
            yield self._lock_hint()
            return
        if self._blocked(event):
            yield "这里不能动鼠标。"
            return
        if not self._rate_ok():
            yield "操作太频繁了，慢一点。"
            return
        try:
            await asyncio.to_thread(
                dio.click, int(x), int(y), str(button or "left"), str(double).lower() == "true"
            )
            yield f"点了 ({x}, {y})。"
        except Exception as exc:
            yield f"点击失败：{exc}"

    @filter.llm_tool(name="pc_type")
    async def pc_type(self, event: AstrMessageEvent, text: str):
        """在光标当前位置打字。

        Args:
            text(string): 要输入的文字
        """
        if not self._may_modify():
            yield self._lock_hint()
            return
        if self._blocked(event):
            yield "这里不能打字。"
            return
        if not self._rate_ok():
            yield "操作太频繁了，慢一点。"
            return
        try:
            await asyncio.to_thread(dio.type_text, str(text))
            yield "打完了。"
        except Exception as exc:
            yield f"输入失败：{exc}"

    @filter.llm_tool(name="pc_key")
    async def pc_key(self, event: AstrMessageEvent, key: str, times: str = "1"):
        """按一个键，比如 enter、esc、tab、space、backspace、up、down。

        Args:
            key(string): 键名
            times(string): 按几次，默认 1
        """
        if not self._may_modify():
            yield self._lock_hint()
            return
        if self._blocked(event):
            yield "这里不能按键。"
            return
        if not self._rate_ok():
            yield "操作太频繁了，慢一点。"
            return
        try:
            await asyncio.to_thread(dio.press, str(key), int(times or 1))
            yield f"按了 {key}。"
        except Exception as exc:
            yield f"按键失败：{exc}"

    @filter.llm_tool(name="pc_hotkey")
    async def pc_hotkey(self, event: AstrMessageEvent, keys: str):
        """按组合键，比如 "ctrl+c"、"ctrl+v"、"alt+tab"。

        Args:
            keys(string): 用 + 连接，例如 ctrl+c
        """
        if not self._may_modify():
            yield self._lock_hint()
            return
        if self._blocked(event):
            yield "这里不能按键。"
            return
        if not self._rate_ok():
            yield "操作太频繁了，慢一点。"
            return
        parts = [p.strip() for p in str(keys or "").split("+") if p.strip()]
        if not parts:
            yield "没给组合键。"
            return
        try:
            await asyncio.to_thread(dio.hotkey, *parts)
            yield f"按了 {'+'.join(parts)}。"
        except Exception as exc:
            yield f"组合键失败：{exc}"

    @filter.llm_tool(name="pc_open")
    async def pc_open(self, event: AstrMessageEvent, app: str):
        """打开一个程序（记事本、计算器、画图、文件管理器、浏览器）。

        Args:
            app(string): 程序名，例如 notepad、calc、mspaint、explorer、edge
        """
        if not self._may_modify():
            yield self._lock_hint()
            return
        if self._blocked(event):
            yield "这里不能开程序。"
            return
        if not self._cfg.get("allow_shell", False):
            yield "开程序的功能没开。"
            return
        name = str(app or "").strip().lower()
        exe = ALLOWED_APPS.get(name)
        if not exe:
            yield f"只能开这些：{', '.join(sorted(ALLOWED_APPS))}"
            return
        if not self._rate_ok():
            yield "操作太频繁了，慢一点。"
            return
        try:
            subprocess.Popen([exe], creationflags=0x08000000)
            yield f"打开了 {name}。"
        except Exception as exc:
            yield f"打开失败：{exc}"

    # ------------------------------------------------------------------ 让模型看到截图

    @filter.on_llm_request()
    async def attach_screenshot(self, event: AstrMessageEvent, req):
        # 强提示：他明确让看屏幕时，逼她真的调工具，别只动嘴
        try:
            text = (getattr(event, "message_str", "") or "")
            hints = ("屏幕", "看看", "看一下", "瞧一眼", "截图", "桌面", "我电脑")
            looks = any(h in text for h in hints)
            acts = any(h in text for h in ("点一下", "点开", "打字", "输入", "按一下", "打开"))
            icons = any(h in text for h in ("图标", "整理桌面", "排列", "自动排列"))
            if icons and not self._blocked(event):
                req.system_prompt = (req.system_prompt or "") + (
                    "\n\n【这一轮·整理桌面图标怎么做】Windows 没有直接命令，要走菜单：\n"
                    "1) 先按 Win+D 回到桌面（调用 pc_hotkey 传 win+d，需要修改权限）\n"
                    "2) 在桌面空白处右键（pc_click 传 button=right，坐标选一块没图标的地方）\n"
                    "3) 调 pc_look 看菜单，找到「查看」或「排序方式」\n"
                    "4) 点进去，再点「自动排列图标」或「名称」\n"
                    "5) 每点一步都截图确认，别盲点。"
                )
            wants_image = any(h in text for h in ("截图", "发我", "发给我", "发过来", "给我看"))
            if wants_image and not self._blocked(event):
                req.system_prompt = (req.system_prompt or "") + (
                    "\n\n【这一轮】他要你把屏幕发给他。直接调用 pc_send_screen 截图发过去，"
                    "不要说你发不了。"
                )
            elif looks and not self._blocked(event):
                req.system_prompt = (req.system_prompt or "") + (
                    "\n\n【这一轮】他明确让你看电脑了。你现在就调用 pc_look 截图，"
                    "不许只嘴上说「我看看」而不真的看。看完用一两句话说你看到了什么。"
                )
            if acts and not self._blocked(event) and not self._may_modify():
                req.system_prompt = (req.system_prompt or "") + (
                    "\n\n【这一轮】他要你动手，但你还没拿到修改权限。"
                    "直接告诉他：先发 /允许修改，我才能动鼠标键盘。"
                )
        except Exception:
            pass

        if not self._pending:
            return
        path = Path(self._pending)
        self._pending = ""
        try:
            if not path.exists():
                return
            raw = await asyncio.to_thread(path.read_bytes)
            b64 = base64.b64encode(raw).decode()
            images = getattr(req, "image_urls", None)
            if images is None:
                req.image_urls = []
                images = req.image_urls
            images.append(f"data:image/jpeg;base64,{b64}")
        except Exception as exc:
            logger.warning(f"[电脑手脚] 附加截图失败：{exc}")
