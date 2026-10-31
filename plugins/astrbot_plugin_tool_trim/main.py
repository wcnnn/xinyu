"""工具裁剪：她手里常年攥着六十多个工具，每次说话都在为它们付钱。

实测（2026-10-30）：每轮输入 40,547 tokens，其中 64 个工具的定义占约 35%。
但日常聊天用得上的一直是那十几件。

做法：
1. 把工具按「场景」分组；核心的十几件永远在
2. 看爸爸这句话里有没有场景关键词，有就临时把那组拿出来
3. 场景保持一段时间（默认 10 分钟），免得「嗯」一声工具就没了
4. 输出时核心组在前、场景组在后——前缀稳定，方便服务端缓存命中

唯一要小心的：她要做但手里没有 → 会变成「我做不到」。
所以关键词给得宽，另留一个「全部工具」的开关。
"""

from __future__ import annotations

import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

# ---------------------------------------------------------------- 场景定义

# 工具名前缀 -> 场景
SCENE_PREFIXES: dict[str, tuple[str, ...]] = {
    "douyin": ("douyin_",),
    "pc": ("pc_", "run_command", "ask_to_use_pc"),
    "dsh": ("dsh_connector_",),
    "study": (
        "learn_from_link",
        "read_page",
        "search_papers",
        "recall_learning",
        "remember_learning",
    ),
    "qzone": ("llm_view_feed", "qzone_"),
    "extend": ("request_plugin_install", "apply_plugin_install"),
    # AstrBot 内置的电脑操作 / 文件 / 代码执行 / 技能管道，共 25 个
    "sys": ("astrbot_",),
    # 内置的网络搜索，共 16 个（一个 key 都没配，调了只会报错）
    "net": (
        "web_search_",
        "web_search_serpapi",
        "image_search_serpapi",
        "finance_search_serpapi",
        "scholar_search_serpapi",
        "tavily_extract_web_page",
        "firecrawl_extract_web_page",
        "exa_get_contents",
        "deepseek_fetch_web_page",
    ),
}

# 场景 -> 触发词
SCENE_WORDS: dict[str, tuple[str, ...]] = {
    "douyin": (
        "抖音", "刷", "视频", "短视频", "分享", "发给", "直播间", "直播",
        "作品", "评论", "点赞", "收藏", "博主", "推荐", "刷到",
    ),
    "pc": (
        "打开", "点开", "关掉", "关闭", "输入", "打字", "键盘", "鼠标",
        "屏幕", "桌面", "电脑", "窗口", "程序", "软件", "命令", "终端",
        "帮我点", "帮我开", "操作", "文件夹", "录像", "截图",
    ),
    "dsh": (
        "dsh", "deepseek", "工作区", "会话", "子代理", "预设", "权限",
        "模型", "服务商", "技能",
    ),
    "study": (
        "学习", "学一下", "查资料", "论文", "链接", "读一下", "搜一下",
        "记住这个", "知识点", "教程",
    ),
    "qzone": ("空间", "说说", "动态", "朋友圈", "发条"),
    "extend": ("装插件", "装个", "插件", "扩展", "安装", "功能不好用"),
    "sys": (
        "文件", "文件夹", "代码", "脚本", "运行", "执行", "python", "写个程序",
        "终端", "命令", "下载", "上传", "表格", "数据", "整理一下", "批量",
        "编辑文件", "读一下文件", "浏览器", "网页操作", "自动化", "定时任务",
    ),
    "net": (
        "搜", "搜索", "查一下", "查查", "网上", "联网", "新闻", "资料",
        "百度", "谷歌", "网页", "网址", "最新消息", "限时", "实时",
        "真的假的", "帮我找", "找一下",
    ),
}

# 一句话里出现这些，就当她要全套
ALL_WORDS = ("全部工具", "所有工具", "工具全开", "都给你", "别裁", "全开")


# ---------------------------------------------------------------- 插件

@register(
    "astrbot_plugin_tool_trim",
    "素衣携清风 & DSH",
    "按场景给工具，省上下文",
    "1.0.0",
)
class ToolTrim(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir()
        self._log_path = Path(self.data_dir) / "trim.log"
        self._held: dict[str, float] = {}
        self._last_count = -1

    # -------------------------------------------------- 配置

    def _cfg(self, key: str, default):
        try:
            v = self.config.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def _enabled(self) -> bool:
        v = self._cfg("enabled", True)
        return bool(v)

    def _hold_seconds(self) -> int:
        try:
            return max(0, int(self._cfg("hold_seconds", 600)))
        except Exception:
            return 600

    def _always(self) -> set[str]:
        raw = str(self._cfg("always", "") or "")
        return {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}

    # -------------------------------------------------- 场景判定

    @staticmethod
    def _scene_of(tool_name: str) -> str | None:
        for scene, prefixes in SCENE_PREFIXES.items():
            for p in prefixes:
                if tool_name.startswith(p) or tool_name == p:
                    return scene
        return None

    def _match_scenes(self, text: str) -> set[str]:
        low = (text or "").lower()
        if not low:
            return set()
        hit = set()
        for scene, words in SCENE_WORDS.items():
            for w in words:
                if w in low:
                    hit.add(scene)
                    break
        return hit

    def _active_scenes(self, text: str) -> set[str]:
        now = time.time()
        hit = self._match_scenes(text)

        if hit:
            for s in hit:
                self._held[s] = now

        # 清掉过期的
        keep = self._hold_seconds()
        alive = {s for s, t in self._held.items() if now - t <= keep}
        self._held = {s: self._held[s] for s in alive}

        return alive

    # -------------------------------------------------- 日志

    def _log(self, msg: str) -> None:
        try:
            if self._log_path.stat().st_size > 512 * 1024:
                self._log_path.unlink()
        except Exception:
            pass
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
        except Exception:
            pass

    # -------------------------------------------------- 钩子

    @filter.on_llm_request()
    async def trim_tools(self, event: AstrMessageEvent, req) -> None:
        # 这个钩子挡在她回话的路上，出错会让她直接不说话。
        # 所以里面任何异常都吞掉：裁不成就算了，工具全给她也能活。
        try:
            self._trim(event, req)
        except Exception as e:
            try:
                self._log(f"裁剪失败，本次放全：{e}")
            except Exception:
                pass

    def _trim(self, event: AstrMessageEvent, req) -> None:
        if not self._enabled():
            return

        toolset = getattr(req, "func_tool", None)
        tools = list(getattr(toolset, "tools", None) or [])
        if not tools:
            return

        text = ""
        try:
            text = req.prompt or ""
        except Exception:
            text = ""
        if not text:
            try:
                text = event.message_str or ""
            except Exception:
                text = ""

        # 她要全套，就不动
        low = (text or "").lower()
        if any(w in low for w in ALL_WORDS):
            if self._last_count != len(tools):
                self._log(f"全给：{len(tools)} 个")
                self._last_count = len(tools)
            return

        scenes = self._active_scenes(text)
        always = self._always()

        core, extra = [], []
        for t in tools:
            name = getattr(t, "name", "")
            if not name:
                continue
            scene = self._scene_of(name)
            if scene is None or scene in scenes or name in always:
                (core if (scene is None or name in always) else extra).append(t)

        keep = core + extra
        if not keep:
            return

        if len(keep) == len(tools):
            if self._last_count != len(tools):
                self._log(f"不动：{len(tools)} 个　场景={sorted(scenes) or '-'}")
                self._last_count = len(tools)
            return

        # 核心在前、场景组在后：前缀稳，缓存友好
        try:
            toolset.tools = keep
        except Exception:
            try:
                req.func_tool.tools = keep
            except Exception:
                logger.debug("[tool_trim] 改不动 req.func_tool.tools")
                return

        self._log(
            f"{len(tools)} → {len(keep)}　场景={sorted(scenes) or '-'}　"
            f"省 {len(tools) - len(keep)} 个"
        )
        self._last_count = len(keep)