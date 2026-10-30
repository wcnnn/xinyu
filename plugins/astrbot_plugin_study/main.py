"""自主学习：链接解析 + 文献检索 + 网页阅读。

不启动常驻浏览器：
- 抖音/视频链接用 yt-dlp 取标题、文案、作者、字幕（不下载视频）
- 学术文献走 arXiv 与 Semantic Scholar 的免费接口（无需 key）
- 网页用 aiohttp 抓取后用 BeautifulSoup 抽正文

学到的东西可以写进「糯糯的学习笔记」。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import aiohttp
from bs4 import BeautifulSoup

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DOUYIN_PROFILE = str(
    Path(__file__).resolve().parents[2] / "plugin_data" / "astrbot_plugin_douyin_dm"
    / "browser-profile"
)
ARXIV = "http://export.arxiv.org/api/query"
S2 = "https://api.semanticscholar.org/graph/v1/paper/search"


async def _get(url: str, headers: dict | None = None, timeout: int = 25) -> tuple[int, str]:
    """先正常握手，证书失败再退回不校验（部分网络环境会拦截证书）。"""
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    for insecure in (False, True):
        try:
            connector = aiohttp.TCPConnector(ssl=False) if insecure else None
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers=hdrs,
                connector=connector,
            ) as session:
                async with session.get(url) as resp:
                    return resp.status, await resp.text(errors="ignore")
        except Exception as exc:
            last = exc
    raise last


URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)
DOUYIN_HINT = ("douyin.com", "iesdouyin.com", "v.douyin.com")


def extract_urls(event) -> list[str]:
    """从消息文本和 JSON/卡片组件里挖出所有 URL。"""
    found: list[str] = []

    def collect(text: str) -> None:
        for match in URL_RE.findall(text or ""):
            if match not in found:
                found.append(match.rstrip(".,);]"))

    collect(getattr(event, "message_str", "") or "")
    for comp in (getattr(event, "message", None) or []):
        for attr in ("data", "url", "file", "text"):
            value = getattr(comp, attr, None)
            if isinstance(value, str):
                collect(value)
            elif isinstance(value, dict):
                try:
                    collect(json.dumps(value, ensure_ascii=False))
                except Exception:
                    pass
    return found


def douyin_url(urls: list[str]) -> str:
    for url in urls:
        low = url.lower()
        if any(hint in low for hint in DOUYIN_HINT):
            return url
    return ""


async def resolve_short(url: str, timeout: int = 20) -> str:
    """展开 v.douyin.com 短链。"""
    if "v.douyin.com" not in url.lower():
        return url
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            connector=connector,
            headers={"User-Agent": MOBILE_UA},
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                final = str(resp.url)
                if final and final.startswith("http"):
                    return final
    except Exception as exc:
        logger.info(f"[自主学习] 短链展开失败：{exc}")
    return url


def _trace(msg: str) -> None:
    """临时诊断：把每一步写进 trace.log。"""
    try:
        base = Path(__file__).resolve().parents[2] / "plugin_data" / "astrbot_plugin_study"
        base.mkdir(parents=True, exist_ok=True)
        with open(base / "trace.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def _clean(text: str, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


@register(
    "astrbot_plugin_study",
    "local",
    "自主学习：链接解析、文献检索、网页阅读",
    "v1.0.0",
)
class StudyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @property
    def _cfg(self) -> dict:
        return getattr(self, "config", None) or {}

    @staticmethod
    def _is_group(event) -> bool:
        try:
            message_type = event.get_message_type()
        except Exception:
            return False
        return "GROUP" in (getattr(message_type, "name", "") or str(message_type)).upper()

    def _blocked(self, event) -> bool:
        return bool(self._cfg.get("private_only", True)) and self._is_group(event)

    # ------------------------------------------------------------------ 知识库

    async def _save_note(self, title: str, body: str, source: str) -> None:
        if not self._cfg.get("save_to_kb", True):
            return
        kb_name = str(self._cfg.get("learning_kb") or "").strip()
        if not kb_name:
            return
        try:
            kb = await self.context.kb_manager.get_kb_by_name(kb_name)
            if kb is None:
                logger.info(f"[自主学习] 知识库「{kb_name}」不存在，跳过入库")
                return
            stamp = time.strftime("%Y%m%d-%H%M%S")
            doc = (
                f"# {title}\n\n"
                f"- 时间：{time.strftime('%Y-%m-%d %H:%M')}\n"
                f"- 来源：{source}\n\n"
                f"{body}\n"
            )
            await kb.upload_document(
                file_name=f"study-{stamp}.md",
                file_content=doc.encode("utf-8"),
                file_type="md",
            )
        except Exception as exc:
            logger.warning(f"[自主学习] 写入知识库失败：{exc}")

    # ------------------------------------------------------------------ 链接解析

    @staticmethod
    def _ytdlp_info(url: str) -> dict:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": False,
            "extract_flat": False,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}

    async def _parse_video(self, url: str) -> dict:
        try:
            info = await asyncio.wait_for(asyncio.to_thread(self._ytdlp_info, url), timeout=90)
        except Exception as exc:
            logger.info(f"[自主学习] yt-dlp 解析失败：{exc}")
            return {}
        return {
            "title": info.get("title") or "",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "description": info.get("description") or "",
            "duration": info.get("duration") or 0,
            "webpage": info.get("webpage_url") or url,
        }

    async def _capture_frames(self, url: str, count: int = 3) -> list[str]:
        """用登录态无头浏览器截几张视频画面。"""
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            _trace(f"frames: playwright missing {exc}")
            return []
        shots: list[str] = []
        try:
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    DOUYIN_PROFILE, channel="msedge", headless=True
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                    await page.wait_for_timeout(3000)
                    video = page.locator("video").first
                    if await video.count() == 0:
                        _trace("frames: no video element")
                        return []
                    try:
                        await video.click(timeout=3000)
                    except Exception:
                        pass
                    out_dir = Path(
                        str(Path(__file__).resolve().parents[2])
                    )
                    out_dir.mkdir(parents=True, exist_ok=True)
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    for i in range(count):
                        await page.wait_for_timeout(2000)
                        target = out_dir / f"{stamp}-{i}.jpg"
                        await video.screenshot(path=str(target), type="jpeg", quality=70)
                        shots.append(str(target))
                    _trace(f"frames captured: {len(shots)}")
                finally:
                    await ctx.close()
        except Exception as exc:
            _trace(f"frames fail {type(exc).__name__}: {exc}"[:200])
        return shots

    async def _describe_frames(self, paths: list[str]) -> str:
        """把截到的画面交给视觉模型描述。"""
        if not paths:
            return ""
        try:
            import base64

            from astrbot.core.agent.message import ImageURLPart, Message, TextPart

            parts = [TextPart(text="这是同一条抖音视频的几张画面，用两三句话描述画面里在发生什么，只描述看到的内容。")]
            for path in paths[:3]:
                with open(path, "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode()
                parts.append(ImageURLPart(image_url={"url": f"data:image/jpeg;base64,{b64}"}))
            resp = await self.context.llm_generate(
                contexts=[Message(role="user", content=parts)], max_tokens=300
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            _trace(f"describe ok {text[:60]}")
            return text
        except Exception as exc:
            _trace(f"describe fail {type(exc).__name__}: {exc}"[:200])
            return ""

    async def _render_douyin(self, url: str) -> dict:
        """用抖音插件的登录态 profile 无头渲染分享页，取标题与文案。"""
        _trace(f"render start {url[:80]}")
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            _trace(f"playwright import fail {exc}")
            logger.info(f"[自主学习] playwright 不可用：{exc}")
            return {}
        js = (
            "() => { const m = (n) => { const e = document.querySelector('meta[name=\"'+n+'\"]')"
            " || document.querySelector('meta[property=\"'+n+'\"]'); return e ? e.content : ''; };"
            " return { title: document.title, desc: m('description'), ogtitle: m('og:title') }; }"
        )
        try:
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    DOUYIN_PROFILE, channel="msedge", headless=True
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                    await page.wait_for_timeout(4500)
                    info = await page.evaluate(js)
                    _trace(f"render ok title={str(info.get('title'))[:40]}")
                finally:
                    await ctx.close()
        except Exception as exc:
            _trace(f"render fail {type(exc).__name__}: {exc}"[:300])
            logger.info(f"[自主学习] 抖音渲染失败：{exc}")
            return {}
        title = str(info.get("title") or "").replace(" - 抖音", "").strip()
        desc = str(info.get("desc") or "").strip()
        return {"title": title, "desc": desc}

    async def _douyin_meta(self, url: str) -> dict:
        """yt-dlp 拿不到时，用手机 UA 抓页面 meta。"""
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25),
                connector=connector,
                headers={"User-Agent": MOBILE_UA},
            ) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    html = await resp.text(errors="ignore")
        except Exception as exc:
            logger.info(f"[自主学习] 抖音 meta 抓取失败：{exc}")
            return {}
        soup = BeautifulSoup(html, "lxml")

        def meta(name: str) -> str:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            return (tag.get("content") or "").strip() if tag else ""

        title = meta("og:title") or meta("title")
        desc = meta("og:description") or meta("description")
        if not title and not desc:
            text = soup.get_text(" ")
            desc = _clean(text, 400)
        return {"title": title, "uploader": "", "description": desc, "webpage": url}

    async def _parse_page(self, url: str) -> str:
        try:
            _, html = await _get(url, timeout=25)
        except Exception as exc:
            logger.info(f"[自主学习] 抓取失败：{exc}")
            return ""
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text("\n")
        text = re.sub(r"\n{2,}", "\n", text)
        return _clean(text, int(self._cfg.get("max_chars", 3000) or 3000))

    @filter.llm_tool(name="learn_from_link")
    async def learn_from_link(self, event: AstrMessageEvent, url: str):
        """解析一个链接（抖音/视频/文章），把内容读进来并记成学习笔记。

        Args:
            url(string): 完整链接，例如 https://v.douyin.com/xxxxx/
        """
        if self._blocked(event):
            yield "群里不学这个。"
            return
        url = str(url or "").strip()
        if not url.startswith("http"):
            yield "这不是一个链接。"
            return

        _trace(f"tool learn_from_link raw={url[:80]}")
        url = await resolve_short(url)
        _trace(f"resolved {url[:90]}")
        if any(hint in url for hint in DOUYIN_HINT):
            rendered = await self._render_douyin(url)
            if rendered.get("desc"):
                body = f"标题：{rendered.get('title', '')}\n文案：{rendered['desc']}"
                shots = await self._capture_frames(url)
                if shots:
                    desc = await self._describe_frames(shots)
                    if desc:
                        body += f"\n画面：{desc}"
                await self._save_note(rendered.get("title") or "抖音", body, url)
                yield body[:2000]
                return
        parts = []
        meta = await self._parse_video(url)
        if not (meta.get("title") or meta.get("description")):
            fallback = await self._douyin_meta(url)
            if fallback.get("title") or fallback.get("description"):
                meta = fallback
        if meta.get("title") or meta.get("description"):
            parts.append(f"标题：{meta['title']}")
            if meta.get("uploader"):
                parts.append(f"作者：{meta['uploader']}")
            if meta.get("description"):
                parts.append(f"文案：{_clean(meta['description'], 800)}")
        page = await self._parse_page(url)
        if page:
            parts.append(f"页面正文节选：{page[:1200]}")
        if not parts:
            yield "这个链接没读出内容，可能被平台拦了，或者需要登录。"
            return

        body = "\n".join(parts)
        await self._save_note(meta.get("title") or "链接学习", body, url)
        yield body[:2000]

    # ------------------------------------------------------------------ 文献检索

    async def _arxiv(self, query: str, limit: int) -> list[dict]:
        params = {
            "search_query": f"all:{query}",
            "start": "0",
            "max_results": str(limit),
            "sortBy": "relevance",
        }
        url = ARXIV + "?" + urllib.parse.urlencode(params)
        try:
            status, text = await _get(url)
            if status != 200:
                logger.info(f"[自主学习] arXiv HTTP {status}")
                return []
        except Exception as exc:
            logger.info(f"[自主学习] arXiv 失败：{exc}")
            return []
        rows = []
        try:
            ns = {"a": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(text)
            for entry in root.findall("a:entry", ns)[:limit]:
                title = _clean(entry.findtext("a:title", default="", namespaces=ns), 200)
                summary = _clean(entry.findtext("a:summary", default="", namespaces=ns), 500)
                link = entry.findtext("a:id", default="", namespaces=ns)
                rows.append({"title": title, "abstract": summary, "url": link, "source": "arXiv"})
        except Exception as exc:
            logger.info(f"[自主学习] arXiv 解析失败：{exc}")
        return rows

    async def _semantic(self, query: str, limit: int) -> list[dict]:
        params = {
            "query": query,
            "limit": str(limit),
            "fields": "title,abstract,year,authors,url",
        }
        url = S2 + "?" + urllib.parse.urlencode(params)
        try:
            status, text = await _get(url, {"Accept": "application/json"})
            if status != 200:
                logger.info(f"[自主学习] Semantic Scholar HTTP {status}")
                return []
            data = json.loads(text)
        except Exception as exc:
            logger.info(f"[自主学习] Semantic Scholar 失败：{exc}")
            return []
        rows = []
        for item in (data.get("data") or [])[:limit]:
            authors = ", ".join(a.get("name", "") for a in (item.get("authors") or [])[:3])
            rows.append({
                "title": item.get("title") or "",
                "abstract": _clean(item.get("abstract") or "", 500),
                "url": item.get("url") or "",
                "year": item.get("year"),
                "authors": authors,
                "source": "Semantic Scholar",
            })
        return rows

    async def _openalex(self, query: str, limit: int) -> list[dict]:
        params = {"search": query, "per-page": str(limit)}
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        try:
            status, text = await _get(url, {"Accept": "application/json"})
            if status != 200:
                logger.info(f"[自主学习] OpenAlex HTTP {status}")
                return []
            data = json.loads(text)
        except Exception as exc:
            logger.info(f"[自主学习] OpenAlex 失败：{exc}")
            return []
        rows = []
        for item in (data.get("results") or [])[:limit]:
            abstract = item.get("abstract_inverted_index") or {}
            words = []
            if abstract:
                slots = {}
                for word, positions in abstract.items():
                    for pos in positions:
                        slots[pos] = word
                words = [slots[k] for k in sorted(slots)]
            rows.append({
                "title": item.get("display_name") or "",
                "abstract": _clean(" ".join(words), 500),
                "url": item.get("doi") or item.get("id") or "",
                "year": item.get("publication_year"),
                "source": "OpenAlex",
            })
        return rows
    @filter.llm_tool(name="search_papers")
    async def search_papers(self, event: AstrMessageEvent, query: str, limit: int = 5):
        """检索学术文献（arXiv + Semantic Scholar，免费无需 key）。

        Args:
            query(string): 检索词，中英文都行，例如 "diffusion model" 或 "大语言模型 记忆"
            limit(number): 返回几篇，默认 5
        """
        if self._blocked(event):
            yield "群里不查文献。"
            return
        query = str(query or "").strip()
        if not query:
            yield "检索词为空。"
            return
        limit = max(1, min(int(limit or 5), 8))
        rows = await self._arxiv(query, limit)
        if len(rows) < limit:
            rows += await self._semantic(query, limit - len(rows))
        if len(rows) < limit:
            rows += await self._openalex(query, limit - len(rows))
        if not rows:
            yield "没检索到，可能网络不通或者引擎限流了。"
            return
        lines = []
        for i, row in enumerate(rows, 1):
            head = f"{i}. {row['title']}（{row['source']}"
            if row.get("year"):
                head += f" {row['year']}"
            head += "）"
            lines.append(head)
            if row.get("abstract"):
                lines.append(f"   {row['abstract'][:220]}")
            if row.get("url"):
                lines.append(f"   {row['url']}")
        text = "\n".join(lines)
        await self._save_note(f"文献检索：{query}", text, "arXiv / Semantic Scholar")
        yield text[:2500]

    # ------------------------------------------------------------------ 网页阅读

    @filter.llm_tool(name="read_page")
    async def read_page(self, event: AstrMessageEvent, url: str):
        """读一个网页的正文（用于阅读文章、文档、百科）。

        Args:
            url(string): 网页地址
        """
        if self._blocked(event):
            yield "群里不读网页。"
            return
        url = str(url or "").strip()
        if not url.startswith("http"):
            yield "这不是一个链接。"
            return
        text = await self._parse_page(url)
        if not text:
            yield "这个页面没读出正文。"
            return
        yield text[:2500]

    @filter.on_llm_request()
    async def auto_parse_shared_link(self, event: AstrMessageEvent, req):
        """手机分享卡片自带的文字就是内容，直接用；不够再尝试联网补。"""
        _trace("hook entered")
        if self._blocked(event):
            _trace("hook blocked")
            return
        try:
            urls = extract_urls(event)
            target = douyin_url(urls)
            _trace(f"hook urls={len(urls)} target={target[:60]}")
        except Exception as exc:
            _trace(f"hook extract fail {exc}"[:200])
            return
        if not target:
            return

        _trace("hook fired")
        raw = getattr(event, "message_str", "") or ""
        text = re.sub(r"<system_reminder>.*?</system_reminder>", " ", raw, flags=re.S)
        for url in urls:
            text = text.replace(url, " ")
        # 去掉复制口令、话题符号这类噪音
        text = re.sub(r"[0-9]+(?:\.[0-9]+)?\s*复制打开抖音[，,：:]?\s*", " ", text)
        text = re.sub(r"[\w@:/.]{2,}\s*[:：]\s*[\w@:./]{2,}\s*[0-9/]{2,}", " ", text)
        text = re.sub(r"\s{2,}", " ", text).strip(" ，,。.\n")

        pieces = []
        if len(text) >= 12:
            pieces.append(f"分享文字：{text[:900]}")
        else:
            resolved = await resolve_short(target)
            meta = await self._parse_video(resolved)
            if not (meta.get("title") or meta.get("description")):
                meta = await self._douyin_meta(resolved) or meta
            if meta.get("title"):
                pieces.append(f"标题：{meta['title']}")
            if meta.get("description"):
                pieces.append(f"文案：{_clean(meta['description'], 600)}")

        if not pieces:
            pieces.append("（只收到一个链接，页面需要登录才能打开，没拿到内容）")

        body = "\n".join(pieces)
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n【他刚分享的抖音卡片】\n" + body[:1500]
            + "\n（这就是卡片里的文字内容，直接用这段回答，不要再读链接、也不要说看不到。）"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("看")
    async def see_command(self, event: AstrMessageEvent):
        """看：/看 <链接>，直接把链接内容读出来。"""
        parts = (getattr(event, "message_str", "") or "").strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法：/看 <链接>")
            return
        url = parts[1].strip()
        if not url.startswith("http"):
            yield event.plain_result("要一个 http 开头的链接")
            return
        if any(hint in url for hint in DOUYIN_HINT):
            resolved = await resolve_short(url)
            rendered = await self._render_douyin(resolved)
            if rendered.get("desc"):
                body = f"标题：{rendered.get('title', '')}\n文案：{rendered['desc']}"
                await self._save_note(rendered.get("title") or "抖音", body, url)
                yield event.plain_result(body[:1800])
                return
            yield event.plain_result("渲染没拿到内容（日志 trace.log 里有原因）")
            return
        text = await self._parse_page(url)
        yield event.plain_result(text[:1800] if text else "这个页面没读出正文。")

