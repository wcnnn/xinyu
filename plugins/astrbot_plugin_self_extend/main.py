"""自我扩展：她申请装插件，爸爸说一声"同意"就批准。

流程：
1. 她说"我想装个X插件" → 调 request_plugin_install(来源, 理由)
2. 插件记下申请，回她："等爸爸同意"
3. 爸爸发 /同意（不用口令）→ 批准
4. 她才能调 apply_plugin_install 真正安装

支持来源：
- 插件市场名（astrbot_plugin_xxx）→ 走 GitHub
- GitHub / Gitee / GitLab 仓库地址 → 走 git 克隆
- zip 压缩包直链 → 走 URL 安装

所有动作写进 audit.log。
"""

from __future__ import annotations

import datetime
import json
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

RE_MARKET_NAME = re.compile(r"^[A-Za-z0-9_\-]{3,80}$")
RE_GIT_URL = re.compile(
    r"^https?://(github\.com|gitee\.com|gitlab\.com|raw\.githubusercontent\.com)/[^\s]{3,300}$"
)
RE_ZIP_URL = re.compile(r"^https?://[^\s]+\.zip$")
RE_GIT_SSH = re.compile(r"^git@[A-Za-z0-9._\-]+:[A-Za-z0-9._\-/]{3,200}$")

MAX_PENDING_MINUTES = 120

# 国内下载 GitHub 常用镜像（会拼成 https://mirror/<owner>/<repo>）
DEFAULT_MIRRORS = [
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://github.moeyy.xyz/",
    "https://ghproxy.net/",
    "https://ghproxy.cc/",
    "https://mirror.ghproxy.com/",
]

# 国内直连的站点，不加镜像
DOMESTIC_HOSTS = ("gitee.com", "gitlab.com", "gitee.net")


@register(
    "astrbot_plugin_self_extend",
    "local",
    "自我扩展：装插件需爸爸同意",
    "v2.0.0",
)
class SelfExtendPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_self_extend")
        self.audit = self.data_dir / "audit.log"
        self.state_path = self.data_dir / "pending.json"
        self.pending: dict = self._load()

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
                json.dumps(self.pending, ensure_ascii=False, indent=1), "utf-8"
            )
        except Exception as exc:
            logger.warning(f"[自我扩展] 保存状态失败：{exc}")

    def _log(self, text: str) -> None:
        try:
            line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {text}"
            with self.audit.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    @staticmethod
    def _classify(source: str) -> str:
        """判断来源类型：market / git / zip / unknown"""
        src = source.strip()
        if RE_GIT_SSH.match(src):
            return "git"
        if RE_ZIP_URL.match(src):
            return "zip"
        if RE_GIT_URL.match(src):
            return "git"
        if RE_MARKET_NAME.match(src):
            return "market"
        return "unknown"

    # ------------------------------------------------------------------ 工具：申请

    @filter.llm_tool(name="request_plugin_install")
    async def request_plugin_install(self, event: AstrMessageEvent, plugin_name: str, reason: str):
        """申请安装插件，需要爸爸明确同意后才会真正安装。

        Args:
            plugin_name(string): 插件市场里的名字（如 astrbot_plugin_weather），或 GitHub/Gitee 仓库地址，或 zip 直链
            reason(string): 为什么要装它，一句话
        """
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            yield "自我扩展没开。"
            return

        source = str(plugin_name or "").strip()
        kind = self._classify(source)
        if kind == "unknown":
            yield (
                f"这个来源我不能用：{source}\n"
                "只支持：插件市场名、GitHub/Gitee/GitLab 仓库地址、git 地址、zip 直链。"
            )
            return

        self.pending = {
            "source": source,
            "kind": kind,
            "reason": str(reason or "")[:200],
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "approved": False,
        }
        self._save()
        self._log(f"申请 source={source} kind={kind} reason={reason}")

        yield (
            f"我想装「{source}」（{kind}），因为：{reason}\n"
            "等爸爸同意。他发 /同意 我就装，发 /拒绝 就算了。"
        )

    # ------------------------------------------------------------------ 命令：批准 / 拒绝

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("同意")
    async def approve_cmd(self, event: AstrMessageEvent):
        """同意：直接发 /同意 —— 批准她刚才的安装申请。"""
        if not self.pending:
            yield event.plain_result("现在没有待批准的申请。")
            return

        created = self.pending.get("created") or ""
        try:
            age = (datetime.datetime.now() - datetime.datetime.fromisoformat(created)).total_seconds()
        except Exception:
            age = 0
        if age > MAX_PENDING_MINUTES * 60:
            self.pending = {}
            self._save()
            yield event.plain_result("这个申请太久了（超过 2 小时），让她重新提。")
            return

        self.pending["approved"] = True
        self._save()
        self._log(f"已批准 source={self.pending.get('source')}")
        yield event.plain_result(f"同意了，她可以装「{self.pending.get('source')}」了。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拒绝")
    async def reject_cmd(self, event: AstrMessageEvent):
        """拒绝：/拒绝 —— 驳回她刚才的安装申请。"""
        src = (self.pending or {}).get("source", "")
        self.pending = {}
        self._save()
        self._log(f"已拒绝 source={src}")
        yield event.plain_result("拒了。" if src else "现在没有待批准的申请。")

    # ------------------------------------------------------------------ 工具：执行

    @filter.llm_tool(name="apply_plugin_install")
    async def apply_plugin_install(self, event: AstrMessageEvent):
        """执行已获爸爸同意的插件安装（没批准会被拒绝）。"""
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            yield "自我扩展没开。"
            return
        if not self.pending or not self.pending.get("approved"):
            yield "爸爸还没同意，不能装。先调 request_plugin_install 申请。"
            return

        source = self.pending["source"]
        kind = self.pending.get("kind") or "market"
        try:
            result = await self._install(source, kind)
        except Exception as exc:
            self._log(f"安装 {source} 异常 {exc}")
            yield f"装失败了：{exc}"
            return

        self.pending = {}
        self._save()
        self._log(
            f"安装 source={source} ok={result.get('ok')} msg={str(result.get('msg'))[:200]}"
        )
        if result.get("ok"):
            yield f"「{source}」装好了。要让插件生效，还得重启一下 AstrBot。"
        else:
            yield f"「{source}」没装上：{str(result.get('msg'))[:300]}"

    # ------------------------------------------------------------------ 安装实现

    def _mirrors(self) -> list[str]:
        cfg = getattr(self, "config", None) or {}
        raw = str(cfg.get("mirrors") or "").strip()
        if not raw:
            return list(DEFAULT_MIRRORS)
        return [part.strip() for part in raw.split(";") if part.strip()]

    @staticmethod
    def _is_domestic(url: str) -> bool:
        lowered = url.lower()
        return any(host in lowered for host in DOMESTIC_HOSTS)

    async def _install(self, source: str, kind: str) -> dict:
        if kind == "market":
            repo = f"https://github.com/astrbot/{source}"
            return await self._install_with_fallback(repo, "github")
        if kind == "git":
            repo = source[:-4] if source.endswith(".git") else source
            return await self._install_git(repo, "git", mirror=None) if self._is_domestic(repo) \
                else await self._install_with_fallback(repo, "git")
        if kind == "zip":
            if self._is_domestic(source):
                return await self._install_url(source)
            return await self._install_zip_with_fallback(source)
        return {"ok": False, "msg": f"不认识的来源类型：{kind}"}

    async def _install_with_fallback(self, repo: str, transport: str) -> dict:
        """先直连，失败后依次尝试国内镜像。"""
        last = {"ok": False, "msg": "没试过任何源"}
        try:
            result = await self._install_git(repo, transport, mirror=None)
            if result.get("ok"):
                self._log(f"直连成功 {repo}")
                return result
            last = result
            self._log(f"直连失败 {repo} msg={str(result.get('msg'))[:120]}")
        except Exception as exc:
            last = {"ok": False, "msg": str(exc)}
            self._log(f"直连异常 {repo} {exc}")

        for mirror in self._mirrors():
            try:
                result = await self._install_git(repo, transport, mirror=mirror)
                if result.get("ok"):
                    self._log(f"镜像成功 {mirror} {repo}")
                    return {**result, "msg": f"{result.get('msg')}（经镜像 {mirror}）"}
                last = result
                self._log(f"镜像失败 {mirror} msg={str(result.get('msg'))[:120]}")
            except Exception as exc:
                last = {"ok": False, "msg": str(exc)}
                self._log(f"镜像异常 {mirror} {exc}")
        return last

    async def _install_zip_with_fallback(self, url: str) -> dict:
        last = {"ok": False, "msg": "没试过任何源"}
        try:
            result = await self._install_url(url)
            if result.get("ok"):
                return result
            last = result
        except Exception as exc:
            last = {"ok": False, "msg": str(exc)}
        for mirror in self._mirrors():
            try:
                result = await self._install_url(mirror + url)
                if result.get("ok"):
                    self._log(f"zip 镜像成功 {mirror} {url}")
                    return {**result, "msg": f"{result.get('msg')}（经镜像 {mirror}）"}
                last = result
            except Exception as exc:
                last = {"ok": False, "msg": str(exc)}
        return last

    async def _dashboard_token(self) -> tuple[str, int]:
        """从本机配置签一个短期 token。"""
        import base64
        import hashlib
        import hmac
        import json as _json
        import time
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        cfg = _json.loads((root / "data" / "cmd_config.json").read_text(encoding="utf-8-sig"))
        dash = cfg.get("dashboard") or {}
        secret = str(dash.get("jwt_secret") or "").encode()
        user = str(dash.get("username") or "")
        port = int(cfg.get("port") or 6180)

        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        header = b64(_json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        payload = b64(
            _json.dumps(
                {"username": user, "exp": int(time.time()) + 600}, separators=(",", ":")
            ).encode()
        )
        sig = b64(hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest())
        return f"{header}.{payload}.{sig}", port

    async def _post_api(self, path: str, body: dict) -> dict:
        import asyncio
        import json as _json
        import urllib.request

        token, port = await self._dashboard_token()

        def _call():
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1{path}",
                data=_json.dumps(body).encode(),
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                return _json.loads(resp.read().decode())

        data = await asyncio.to_thread(_call)
        return {
            "ok": str(data.get("status")) == "ok",
            "msg": data.get("message") or data.get("data") or data,
        }

    async def _install_git(self, repo: str, transport: str, mirror: str | None = None) -> dict:
        body = {"repository": repo, "install_method": "github" if transport == "github" else "git"}
        if mirror:
            body["proxy"] = mirror
        return await self._post_api(f"/plugins/install/{transport}", body)

    async def _install_url(self, url: str) -> dict:
        return await self._post_api("/plugins/install/url", {"url": url, "download_url": url})

    # ------------------------------------------------------------------ 提示

    @filter.on_llm_request()
    async def remind(self, event: AstrMessageEvent, req):
        cfg = getattr(self, "config", None) or {}
        if not cfg.get("enabled", True):
            return
        state = "已批准，可以装了" if (self.pending or {}).get("approved") else "未批准"
        guide = (
            "\n\n【关于装插件】你没有直接装插件的权限。想装就先调 request_plugin_install 申请"
            "（可以给插件名、GitHub/Gitee 地址、git 地址或 zip 直链），"
            "等爸爸发 /同意 后，再调 apply_plugin_install。"
            f"当前申请状态：{state}。"
            "不许自己用 shell 或写文件的方式绕过，那是越界。"
        )
        req.system_prompt = (req.system_prompt or "") + guide