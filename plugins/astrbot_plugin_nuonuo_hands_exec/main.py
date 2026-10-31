"""糯糯的执行之手：让她能自己运行命令、跑脚本。

权限设计：
- 默认关闭，需要主人发「开手」才放开
- 危险命令黑名单（格式化、删系统、关机等）
- 每次执行都记录时间和命令
- 超时保护
"""

from __future__ import annotations

import asyncio
import datetime
import json
import subprocess
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

DANGEROUS = (
    "format ", "format.com", "diskpart", "mkfs",
    "rm -rf /", "rm -rf c:", "del /f /s /q c:",
    "shutdown /s", "shutdown /r", "shutdown -s", "shutdown -r",
    "reg delete hklm", "reg delete hkcu",
    "bcdedit", "cacls", "takeown /f c:",
    "net user ", "net localgroup",
    "vssadmin delete", "wbadmin delete",
)

MAX_OUTPUT = 3000
DEFAULT_TIMEOUT = 60


@register(
    "astrbot_plugin_nuonuo_hands_exec",
    "local",
    "让糯糯能自己执行命令",
    "v1.0.0",
)
class HandsExecPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_nuonuo_hands_exec")
        self.state_path = self.data_dir / "state.json"
        self.log_path = self.data_dir / "exec.log"
        self.state = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text("utf-8"))
            return {"enabled": bool(d.get("enabled")), "owner_only": True}
        except Exception:
            return {"enabled": False, "owner_only": True}

    def _save(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8")
        except Exception:
            pass

    # ---------------- 开关 ----------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开手")
    async def enable_hand(self, event: AstrMessageEvent):
        """开手：允许她自己执行命令。"""
        self.state["enabled"] = True
        self._save()
        self._log("开手")
        yield event.plain_result("好，手放开了。现在我能自己跑命令了。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("收手")
    async def disable_hand(self, event: AstrMessageEvent):
        """收手：收回执行命令的权限。"""
        self.state["enabled"] = False
        self._save()
        self._log("收手")
        yield event.plain_result("手收回来了。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("手状态")
    async def hand_status(self, event: AstrMessageEvent):
        """手状态：看看现在能不能执行命令。"""
        on = self.state.get("enabled")
        yield event.plain_result(
            f"手现在{'是放开的' if on else '是收着的'}。" if on
            else "手现在收着。要放开就发「开手」")

    def _may_exec(self, event: AstrMessageEvent) -> bool:
        if not self.state.get("enabled"):
            return False
        # 白名单：只有名单里的 QQ 能用这条命令通道。留空则退回「只看是不是私聊」。
        allow = str(self.state.get("allow_qq") or "").strip()
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            return False                      # 拿不到身份就别放行
        if allow:
            names = [x.strip() for x in allow.replace("，", ",").split(",") if x.strip()]
            return sender in names
        if self.state.get("owner_only", True):
            try:
                mtype = event.get_message_type()
                name = str(getattr(mtype, "value", mtype))
            except Exception:
                return False                  # 判不出类型就别放行（原来这里是放行）
            if "Friend" not in name:
                return False
        return True

    # ---------------- 执行 ----------------
    @filter.llm_tool(name="run_command")
    async def run_command(self, event: AstrMessageEvent, command: str,
                          cwd: str = "", timeout: int = 0):
        """在电脑上执行一条 PowerShell 命令。查状态、跑脚本、看文件、改东西都行。

        Args:
            command(string): 要执行的命令，比如 Get-Process 或 python xxx.py
            cwd(string): 在哪个目录执行，留空用用户主目录
            timeout(int): 最多等多少秒，默认 60，最大 300
        """
        if not self._may_exec(event):
            yield "手现在没放开，主人要发「开手」才行。"
            return

        cmd = str(command or "").strip()
        if not cmd:
            yield "命令是空的。"
            return
        if len(cmd) > 2000:
            yield "命令太长了。"
            return

        low = cmd.lower()
        for pat in DANGEROUS:
            if pat in low:
                self._log(f"拒绝危险命令 {cmd[:80]}")
                yield f"这条太危险了，我不做。"
                return

        t = max(5, min(300, int(timeout) or DEFAULT_TIMEOUT))
        work = str(cwd).strip() or str(Path.home())
        if not Path(work).exists():
            work = str(Path.home())

        self._log(f"执行 {cmd[:100]}")
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=work, creationflags=0x08000000,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=t)
            except asyncio.TimeoutError:
                proc.kill()
                yield f"跑了 {t} 秒还没完，我停了。"
                return
            text = (out or b"").decode("utf-8", errors="replace").strip()
            if not text:
                yield "跑完了，没有输出。"
                return
            if len(text) > MAX_OUTPUT:
                text = text[:MAX_OUTPUT] + f"\n…（还有 {len(text) - MAX_OUTPUT} 字）"
            yield f"$ {cmd[:60]}\n{text}"
        except Exception as exc:
            yield f"跑失败了：{type(exc).__name__}: {exc}"

    def _log(self, text: str) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
        except Exception:
            pass