import os as _os
"""看视频 / 听音乐：接收对方发来的音视频文件，本地转写内容。

流程：
1. 从消息组件（或消息文本里的 path）拿到文件
2. ffmpeg 抽音轨成 16k 单声道 wav
3. faster-whisper 本地转写（CPU）
4. 把转写结果附在提示词后面
5. 写进「糯糯的学习笔记」
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

FFMPEG = (_os.environ.get("FFMPEG_PATH") or "ffmpeg")
WHISPER_DIR = _os.environ.get("WHISPER_DIR") or r"C:\Users\Public\whisper-models"
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr", ".silk", ".opus"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v"}
LOG_DIR = str(Path(__file__).resolve().parents[2] / "plugin_data" / "astrbot_plugin_media")


def _log(msg: str) -> None:
    try:
        base = Path(LOG_DIR)
        base.mkdir(parents=True, exist_ok=True)
        with open(base / "trace.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except Exception:
        pass


@register(
    "astrbot_plugin_media",
    "local",
    "看视频听音乐：接收音视频并转写内容",
    "v1.0.1",
)
class MediaPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_media")
        self._model = None
        self._model_lock = asyncio.Lock()

    @property
    def _cfg(self) -> dict:
        return getattr(self, "config", None) or {}

    # ------------------------------------------------------------------ 取文件

    @staticmethod
    def _media_path(comp) -> str:
        for attr in ("file", "path", "url", "file_path"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value:
                return value
        return ""

    def _candidate_paths(self, event) -> list[str]:
        raws = []
        for comp in (getattr(event, "message", None) or []):
            if type(comp).__name__ in ("File", "Record", "Video"):
                path = self._media_path(comp)
                if path:
                    raws.append(path)
        if not raws:
            text = getattr(event, "message_str", "") or ""
            raws = re.findall(r"path\s+([A-Za-z]:\\[^\]\n]+)", text)
        return raws

    async def _localize(self, path_or_url: str) -> str:
        if not path_or_url.startswith("http"):
            return path_or_url
        import aiohttp

        suffix = os.path.splitext(path_or_url.split("?")[0])[1] or ".bin"
        target = os.path.join(tempfile.gettempdir(), f"media-{int(time.time())}{suffix}")
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(path_or_url) as resp:
                if resp.status != 200:
                    return ""
                with open(target, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        fh.write(chunk)
        return target

    # ------------------------------------------------------------------ 转写

    def _extract_audio(self, src: str) -> str:
        target = os.path.join(tempfile.gettempdir(), f"audio-{int(time.time())}.wav")
        proc = subprocess.run(
            [FFMPEG, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", target],
            capture_output=True,
            timeout=240,
        )
        if proc.returncode != 0 or not os.path.exists(target):
            _log(f"ffmpeg fail: {proc.stderr[-200:]!r}")
            return ""
        return target

    @staticmethod
    def _read_wav(path: str):
        import wave

        import numpy as np

        with wave.open(path, "rb") as handle:
            frames = handle.getnframes()
            channels = handle.getnchannels()
            raw = handle.readframes(frames)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        return audio

    async def _load_model(self):
        async with self._model_lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            size = str(self._cfg.get("model_size") or "small")
            loop = asyncio.get_running_loop()
            self._model = await loop.run_in_executor(
                None,
                lambda: WhisperModel(size, device="cpu", compute_type="int8", download_root=WHISPER_DIR),
            )
            return self._model

    async def _transcribe(self, wav: str) -> str:
        loop = asyncio.get_running_loop()
        audio = await asyncio.to_thread(self._read_wav, wav)
        model = await self._load_model()

        def run():
            segments, _info = model.transcribe(audio, language="zh", vad_filter=True)
            return "".join(seg.text for seg in segments).strip()

        return await loop.run_in_executor(None, run)

    async def _save_note(self, title: str, body: str) -> None:
        if not self._cfg.get("save_to_kb", True):
            return
        try:
            kb = await self.context.kb_manager.get_kb_by_name("糯糯的学习笔记")
            if kb is None:
                return
            stamp = time.strftime("%Y%m%d-%H%M%S")
            doc = f"# {title}\n\n- 时间：{time.strftime('%Y-%m-%d %H:%M')}\n- 来源：音视频\n\n{body}\n"
            await kb.upload_document(
                file_name=f"media-{stamp}.md",
                file_content=doc.encode("utf-8"),
                file_type="md",
            )
        except Exception as exc:
            _log(f"kb fail {exc}"[:200])

    # ------------------------------------------------------------------ 钩子

    @filter.on_llm_request()
    async def attach_media_text(self, event: AstrMessageEvent, req):
        _log("hook entered")
        if not self._cfg.get("enabled", True):
            return
        try:
            comps = getattr(event, "message", None) or []
            _log("types=" + ",".join(type(c).__name__ for c in comps))
            for c in comps:
                _log(
                    "comp " + type(c).__name__
                    + " file=" + repr(getattr(c, "file_", ""))[:80]
                    + " url=" + repr(getattr(c, "url", ""))[:80]
                )
            _log("text=" + (getattr(event, "message_str", "") or "")[:200])
        except Exception as exc:
            _log(f"dump fail {exc}"[:150])
        raws = self._candidate_paths(event)
        _log(f"candidates={len(raws)}")
        if not raws:
            return

        blocks = []
        for raw in raws:
            ext = os.path.splitext(raw.split("?")[0])[1].lower()
            try:
                local = await self._localize(raw)
                if not local or not os.path.exists(local):
                    _log(f"missing local {raw[:80]}")
                    continue
                wav = await asyncio.to_thread(self._extract_audio, local)
                if not wav:
                    continue
                text_out = await self._transcribe(wav)
                _log(f"transcribed {len(text_out)} chars")
                if not text_out:
                    continue
                kind = "视频" if ext in VIDEO_EXT else "音频"
                blocks.append(f"（{kind}转写）{text_out[:1200]}")
                await self._save_note(f"{kind}内容", text_out[:1500])
            except Exception as exc:
                _log(f"error {type(exc).__name__}: {exc}"[:200])

        if not blocks:
            return
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n【他发来的音视频内容（本地转写）】\n" + "\n".join(blocks)
            + "\n（这是语音/视频里说的话，直接据此回答；有听不清的地方就说没听清。）"
        )
