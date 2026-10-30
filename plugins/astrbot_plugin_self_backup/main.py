"""自我改动备份。

每隔一段时间检查「糯糯」的人格文本、AstrBot 主配置、各插件配置有没有变化；
一变就自动打一份快照。超过保留天数（默认 7 天）且没被标记「保留」的快照会自动删除。

回档：/备份 回档 <编号>  —— 恢复人格（实时生效），配置文件放回备份目录供手动恢复。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import shutil

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

DATA_REL = r"core\data"


@register(
    "astrbot_plugin_self_backup",
    "local",
    "自我改动备份：每改动一次快照一次，7 天不回档就自动删",
    "v1.0.0",
)
class SelfBackupPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_self_backup")
        self.backups_dir = self.data_dir / "backups"
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ 路径

    @property
    def _cfg(self) -> dict:
        return getattr(self, "config", None) or {}

    def _data_root(self):
        # plugin_data/<plugin>/ -> core/data
        return self.data_dir.parents[1]

    def _cmd_config_path(self):
        return self._data_root() / "cmd_config.json"

    def _plugin_cfg_dir(self):
        return self._data_root() / "config"

    # ------------------------------------------------------------------ 快照

    def _persona(self) -> dict:
        pid = str(self._cfg.get("persona_id") or "糯糯")
        try:
            for item in self.context.persona_manager.personas_v3:
                if item.get("name") == pid:
                    return {
                        "persona_id": pid,
                        "system_prompt": item.get("prompt") or "",
                        "begin_dialogs": item.get("begin_dialogs") or [],
                    }
        except Exception as exc:
            logger.warning(f"[自我备份] 读取人格失败：{exc}")
        return {"persona_id": pid, "system_prompt": "", "begin_dialogs": []}

    def _fingerprint(self) -> str:
        parts = [hashlib.sha256(self._persona()["system_prompt"].encode()).hexdigest()]
        if self._cfg.get("backup_configs", True):
            paths = [self._cmd_config_path()]
            try:
                paths += sorted(self._plugin_cfg_dir().glob("*.json"))
            except Exception:
                pass
            for path in paths:
                try:
                    parts.append(hashlib.sha256(path.read_bytes()).hexdigest())
                except OSError:
                    continue
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text("utf-8"))
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")

    def _new_id(self) -> str:
        base = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate, n = base, 1
        while (self.backups_dir / candidate).exists():
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    async def snapshot(self, reason: str) -> str:
        slot = self._new_id()
        target = self.backups_dir / slot
        target.mkdir(parents=True, exist_ok=True)

        persona = self._persona()
        (target / "persona.json").write_text(
            json.dumps(persona, ensure_ascii=False, indent=1), "utf-8"
        )

        if self._cfg.get("backup_configs", True):
            cmd = self._cmd_config_path()
            if cmd.is_file():
                shutil.copy2(cmd, target / "cmd_config.json")
            cfg_dir = self._plugin_cfg_dir()
            if cfg_dir.is_dir():
                dest = target / "plugin_configs"
                dest.mkdir(exist_ok=True)
                for path in cfg_dir.glob("*.json"):
                    try:
                        shutil.copy2(path, dest / path.name)
                    except OSError:
                        continue

        manifest = {
            "id": slot,
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "persona_id": persona.get("persona_id"),
            "persona_chars": len(persona.get("system_prompt") or ""),
            "fingerprint": self._fingerprint(),
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8"
        )
        state = self._load_state()
        state["fingerprint"] = manifest["fingerprint"]
        self._save_state(state)
        logger.info(f"[自我备份] 已快照 {slot}（{reason}）")
        return slot

    # ------------------------------------------------------------------ 清理

    def _cleanup(self) -> int:
        days = int(self._cfg.get("retention_days", 7) or 7)
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        removed = 0
        for folder in sorted(self.backups_dir.iterdir()):
            if not folder.is_dir():
                continue
            if (folder / "keep").exists():
                continue
            manifest = folder / "manifest.json"
            try:
                stamp = datetime.datetime.fromisoformat(
                    json.loads(manifest.read_text("utf-8")).get("time", "")
                )
            except Exception:
                stamp = datetime.datetime.fromtimestamp(folder.stat().st_mtime)
            if stamp < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed

    # ------------------------------------------------------------------ 列表 / 恢复

    def _entries(self) -> list[dict]:
        rows = []
        for folder in sorted(self.backups_dir.iterdir(), reverse=True):
            if not folder.is_dir():
                continue
            try:
                manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
            except Exception:
                continue
            manifest["kept"] = (folder / "keep").exists()
            manifest["path"] = str(folder)
            rows.append(manifest)
        return rows

    async def restore(self, slot: str) -> str:
        folder = self.backups_dir / slot
        persona_file = folder / "persona.json"
        if not persona_file.is_file():
            return f"找不到备份 {slot} 的人格文件。"
        data = json.loads(persona_file.read_text("utf-8"))
        pid = data.get("persona_id") or "糯糯"
        prompt = data.get("system_prompt") or ""
        if not prompt:
            return "这个备份里的人格是空的，没有恢复。"
        await self.context.persona_manager.update_persona(
            pid, system_prompt=prompt, begin_dialogs=data.get("begin_dialogs") or []
        )
        return (
            f"已回档到 {slot}：人格「{pid}」恢复为 {len(prompt)} 字，实时生效。\n"
            f"（配置备份在 {folder}，需要的话手动拷回 core/data）"
        )

    # ------------------------------------------------------------------ 定时

    async def _tick(self) -> None:
        state = self._load_state()
        current = self._fingerprint()
        if state.get("fingerprint") != current:
            await self.snapshot("检测到人格或配置变化")
            state = self._load_state()
        removed = self._cleanup()
        if removed:
            logger.info(f"[自我备份] 清理了 {removed} 份过期备份")

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as exc:
                logger.warning(f"[自我备份] 检查出错：{exc}")
            await asyncio.sleep(max(60, int(self._cfg.get("check_interval_seconds", 300) or 300)))

    # ------------------------------------------------------------------ 命令

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("备份")
    async def backup_command(self, event: AstrMessageEvent):
        """备份：/备份 列表 | /备份 立即 [说明] | /备份 回档 <编号> | /备份 保留 <编号>"""
        parts = (getattr(event, "message_str", "") or "").strip().split()
        action = parts[1] if len(parts) > 1 else "列表"

        if action in ("立即", "快照"):
            reason = " ".join(parts[2:]) or "手动备份"
            slot = await self.snapshot(reason)
            yield event.plain_result(f"已备份：{slot}")
            return

        if action in ("列表", "list"):
            rows = self._entries()
            if not rows:
                yield event.plain_result("还没有任何备份。")
                return
            lines = [f"共 {len(rows)} 份备份（保留 {self._cfg.get('retention_days', 7)} 天）："]
            for row in rows[:15]:
                flag = "🔒" if row.get("kept") else "  "
                lines.append(
                    f"{flag} {row['id']}  {row['time'][5:16].replace('T', ' ')}  "
                    f"{row.get('persona_chars', 0)} 字  {row.get('reason', '')}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("回档", "恢复"):
            if len(parts) < 3:
                yield event.plain_result("格式：/备份 回档 <编号>（用 /备份 列表 查编号）")
                return
            yield event.plain_result(await self.restore(parts[2]))
            return

        if action in ("保留", "锁定"):
            if len(parts) < 3:
                yield event.plain_result("格式：/备份 保留 <编号>")
                return
            folder = self.backups_dir / parts[2]
            if not folder.is_dir():
                yield event.plain_result("没有这个备份。")
                return
            (folder / "keep").write_text("kept", "utf-8")
            yield event.plain_result(f"{parts[2]} 已标记保留，不会自动删除。")
            return

        yield event.plain_result(
            "用法：/备份 列表 | /备份 立即 [说明] | /备份 回档 <编号> | /备份 保留 <编号>"
        )

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("[自我备份] 已启动，目录：%s" % self.backups_dir)

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
