from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

_VOCUTTS_SKIP_FLAG = "_vocutts_skip_tts"
_SESSION_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 days
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


@dataclass
class SessionTTSConfig:
    """Per-session TTS state and overrides."""

    enabled: bool = False
    voice_id: str | None = None
    prompt_id: str | None = None
    preset: str | None = None
    last_active: float = field(default_factory=time.time)


@register(
    "astrbot_plugin_vocu_tts",
    "Salieri",
    "Vocu TTS: 自动将机器人回复转为语音，支持 TRPG 括号过滤与情绪映射",
    "0.1.0",
)
class VocuTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.sessions: dict[str, SessionTTSConfig] = {}
        self._http: aiohttp.ClientSession | None = None

    async def initialize(self) -> None:
        temp_dir = get_astrbot_temp_path()
        os.makedirs(temp_dir, exist_ok=True)
        self._http = aiohttp.ClientSession()

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    # ── helpers ──────────────────────────────────────────────

    def _get_cfg(self, key: str, default=None):
        val = self.config.get(key)
        if val is None or val == "":
            return default
        return val

    def _get_session(self, umo: str) -> SessionTTSConfig:
        if umo not in self.sessions:
            self.sessions[umo] = SessionTTSConfig()
        session = self.sessions[umo]
        session.last_active = time.time()
        return session

    def _cleanup_stale_sessions(self) -> None:
        now = time.time()
        stale = [
            k
            for k, v in self.sessions.items()
            if now - v.last_active > _SESSION_EXPIRE_SECONDS
        ]
        for k in stale:
            del self.sessions[k]

    def _resolve_voice_id(self, session: SessionTTSConfig) -> str:
        return session.voice_id or self._get_cfg("voice_id", "")

    def _resolve_prompt_id(self, session: SessionTTSConfig) -> str:
        return session.prompt_id or self._get_cfg("prompt_id", "default")

    def _resolve_preset(self, session: SessionTTSConfig) -> str:
        return session.preset or self._get_cfg("preset", "balance")

    # ── bracket processing ───────────────────────────────────

    def _process_text(self, text: str) -> tuple[str, list[int] | None]:
        """Process text according to bracket_mode config.

        Returns (spoken_text, emo_switch_or_none).
        Empty spoken_text signals the caller to skip TTS entirely.
        """
        mode = self._get_cfg("bracket_mode", "strip")
        pattern = self._get_cfg(
            "bracket_pattern",
            r"[（(][^）)]*[）)]|[【\[][^】\]]*[】\]]",
        )

        if mode == "keep":
            return text, None

        try:
            brackets = re.findall(pattern, text)
            spoken_text = re.sub(pattern, "", text)
        except re.error:
            logger.warning(f"VocuTTS: invalid bracket_pattern regex: {pattern}")
            return text, None

        spoken_text = re.sub(r"\s{2,}", " ", spoken_text).strip()

        if not spoken_text:
            return "", None

        if mode == "emotion_hint" and brackets:
            emo = self._extract_emotion(brackets)
            return spoken_text, emo

        return spoken_text, None

    def _extract_emotion(self, brackets: list[str]) -> list[int] | None:
        raw = self._get_cfg("emotion_keywords", "")
        if not raw:
            return None

        if isinstance(raw, str):
            try:
                keyword_map = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("VocuTTS: emotion_keywords JSON parse failed")
                return None
        else:
            keyword_map = raw

        if not isinstance(keyword_map, dict):
            logger.warning(
                f"VocuTTS: emotion_keywords must be a dict, got {type(keyword_map).__name__}"
            )
            return None

        combined = " ".join(brackets)
        # [Anger, Happiness, Neutral, Sadness, ContextualMatch]
        result = [0, 0, 0, 0, 0]
        matched = False
        for keyword, values in keyword_map.items():
            if isinstance(values, list) and len(values) == 5 and keyword in combined:
                result = [max(r, v) for r, v in zip(result, values)]
                matched = True

        return result if matched else None

    # ── vocu API ─────────────────────────────────────────────

    async def _generate_voice(
        self,
        text: str,
        session: SessionTTSConfig,
        emo_switch: list[int] | None = None,
    ) -> str | None:
        """Call Vocu synchronous TTS API. Returns local file path or None."""
        api_key = self._get_cfg("api_key", "")
        if not api_key:
            logger.warning("VocuTTS: api_key not configured")
            return None

        voice_id = self._resolve_voice_id(session)
        if not voice_id:
            logger.warning("VocuTTS: voice_id not configured")
            return None

        base_url = self._get_cfg("api_base_url", "https://v1.vocu.ai").rstrip("/")

        payload: dict = {
            "voiceId": voice_id,
            "text": text,
            "promptId": self._resolve_prompt_id(session),
            "preset": self._resolve_preset(session),
            "break_clone": self._get_cfg("break_clone", True),
            "language": self._get_cfg("language", "auto"),
            "speechRate": self._get_cfg("speech_rate", 1.0),
            "vivid": self._get_cfg("vivid", False),
            "flash": self._get_cfg("flash", False),
        }
        if emo_switch:
            payload["emo_switch"] = emo_switch

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            http = await self._get_http()
            async with http.post(
                f"{base_url}/api/tts/simple-generate",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"VocuTTS: API returned {resp.status}: {body}")
                    return None
                data = await resp.json()

            audio_url = data.get("data", {}).get("audio")
            if not audio_url:
                logger.error(f"VocuTTS: no audio URL in response: {data}")
                return None

            return await self._download_audio(audio_url)
        except Exception:
            logger.error("VocuTTS: voice generation failed", exc_info=True)
            return None

    async def _download_audio(self, url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            logger.error(f"VocuTTS: refusing non-HTTP audio URL: {url}")
            return None

        temp_dir = get_astrbot_temp_path()
        os.makedirs(temp_dir, exist_ok=True)
        path = os.path.join(temp_dir, f"vocutts_{uuid.uuid4()}.mp3")

        try:
            http = await self._get_http()
            async with http.get(url, timeout=_DOWNLOAD_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.error(f"VocuTTS: audio download failed: {resp.status}")
                    return None
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)
            return path
        except Exception:
            logger.error("VocuTTS: audio download failed", exc_info=True)
            return None

    # ── list voices ──────────────────────────────────────────

    async def _list_voices(self) -> list[dict] | None:
        api_key = self._get_cfg("api_key", "")
        if not api_key:
            return None

        base_url = self._get_cfg("api_base_url", "https://v1.vocu.ai").rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            http = await self._get_http()
            async with http.get(
                f"{base_url}/api/voice",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("data", [])
        except Exception:
            logger.error("VocuTTS: list voices failed", exc_info=True)
            return None

    # ── commands ──────────────────────────────────────────────

    @filter.command_group("vocutts")
    def vocutts_group(self):
        """VocuTTS 语音合成"""

    @vocutts_group.command("on")
    async def vocutts_on(self, event: AstrMessageEvent):
        """开启当前会话的 VocuTTS"""
        event.set_extra(_VOCUTTS_SKIP_FLAG, True)
        umo = event.unified_msg_origin
        session = self._get_session(umo)
        session.enabled = True

        api_key = self._get_cfg("api_key", "")
        voice_id = self._resolve_voice_id(session)
        warnings = []
        if not api_key:
            warnings.append("API Key 未配置，请在 WebUI 中设置")
        if not voice_id:
            warnings.append(
                "Voice ID 未配置，请在 WebUI 中设置或使用 /vocutts voice <id>"
            )

        msg = "VocuTTS 已开启。"
        if warnings:
            msg += "\n⚠ " + "\n⚠ ".join(warnings)
        yield event.plain_result(msg)

    @vocutts_group.command("off")
    async def vocutts_off(self, event: AstrMessageEvent):
        """关闭当前会话的 VocuTTS"""
        event.set_extra(_VOCUTTS_SKIP_FLAG, True)
        umo = event.unified_msg_origin
        session = self._get_session(umo)
        session.enabled = False
        yield event.plain_result("VocuTTS 已关闭。")

    @vocutts_group.command("status")
    async def vocutts_status(self, event: AstrMessageEvent):
        """查看当前会话 VocuTTS 状态"""
        event.set_extra(_VOCUTTS_SKIP_FLAG, True)
        umo = event.unified_msg_origin
        session = self._get_session(umo)
        voice_id = self._resolve_voice_id(session)
        preset = self._resolve_preset(session)
        prompt_id = self._resolve_prompt_id(session)
        status = "开启" if session.enabled else "关闭"
        bracket_mode = self._get_cfg("bracket_mode", "strip")

        lines = [
            f"VocuTTS 状态: {status}",
            f"Voice ID: {voice_id or '未设置'}",
            f"Style ID: {prompt_id}",
            f"预设: {preset}",
            f"括号处理: {bracket_mode}",
        ]
        yield event.plain_result("\n".join(lines))

    @vocutts_group.command("voice")
    async def vocutts_voice(self, event: AstrMessageEvent):
        """设置当前会话的声音角色: /vocutts voice <voice_id>"""
        event.set_extra(_VOCUTTS_SKIP_FLAG, True)
        umo = event.unified_msg_origin
        session = self._get_session(umo)
        args = event.message_str.strip()

        if not args:
            current = self._resolve_voice_id(session)
            yield event.plain_result(
                f"当前 Voice ID: {current or '未设置'}\n用法: /vocutts voice <voice_id>"
            )
            return

        session.voice_id = args
        yield event.plain_result(f"已将当前会话的 Voice ID 设为: {args}")

    @vocutts_group.command("voices")
    async def vocutts_voices(self, event: AstrMessageEvent):
        """列出所有可用的声音角色"""
        event.set_extra(_VOCUTTS_SKIP_FLAG, True)
        voices = await self._list_voices()
        if voices is None:
            yield event.plain_result("获取声音列表失败，请检查 API Key 配置。")
            return

        if not voices:
            yield event.plain_result("账号下暂无声音角色。")
            return

        lines = ["可用声音角色:"]
        for v in voices[:20]:
            name = v.get("name", "unknown")
            vid = v.get("id", "")
            version = v.get("version", "")
            status = v.get("status", "")
            styles = v.get("metadata", {}).get("prompts", [])
            style_names = ", ".join(s.get("name", "") for s in styles[:5])
            line = f"  {name} (ID: {vid}, {version}, {status})"
            if style_names:
                line += f"\n    风格: {style_names}"
            lines.append(line)

        if len(voices) > 20:
            lines.append(f"  ... 共 {len(voices)} 个角色")
        yield event.plain_result("\n".join(lines))

    @vocutts_group.command("style")
    async def vocutts_style(self, event: AstrMessageEvent):
        """设置当前会话的声音风格: /vocutts style <prompt_id>"""
        event.set_extra(_VOCUTTS_SKIP_FLAG, True)
        umo = event.unified_msg_origin
        session = self._get_session(umo)
        args = event.message_str.strip()

        if not args:
            current = self._resolve_prompt_id(session)
            yield event.plain_result(
                f"当前 Style ID: {current}\n用法: /vocutts style <prompt_id>"
            )
            return

        session.prompt_id = args
        yield event.plain_result(f"已将当前会话的 Style ID 设为: {args}")

    @vocutts_group.command("preset")
    async def vocutts_preset(self, event: AstrMessageEvent):
        """设置生成预设: /vocutts preset <creative|balance|stable>"""
        event.set_extra(_VOCUTTS_SKIP_FLAG, True)
        umo = event.unified_msg_origin
        session = self._get_session(umo)
        args = event.message_str.strip()
        valid = {"creative", "balance", "stable"}

        if not args or args not in valid:
            current = self._resolve_preset(session)
            yield event.plain_result(
                f"当前预设: {current}\n"
                f"用法: /vocutts preset <{'|'.join(sorted(valid))}>"
            )
            return

        session.preset = args
        yield event.plain_result(f"已将当前会话的预设设为: {args}")

    # ── after_message_sent hook ──────────────────────────────

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """Intercept sent messages and follow up with TTS voice."""
        if event.get_extra(_VOCUTTS_SKIP_FLAG, False):
            return

        umo = event.unified_msg_origin
        session = self.sessions.get(umo)
        if not session or not session.enabled:
            return

        session.last_active = time.time()
        self._cleanup_stale_sessions()

        result = event.get_result()
        if not result or not result.chain:
            return

        # Skip if the chain already contains audio
        if any(isinstance(comp, Comp.Record) for comp in result.chain):
            return

        text_parts: list[str] = []
        for comp in result.chain:
            if isinstance(comp, Comp.Plain) and comp.text:
                text_parts.append(comp.text)

        full_text = "".join(text_parts).strip()
        if not full_text:
            return

        spoken_text, emo_switch = self._process_text(full_text)
        if not spoken_text:
            return

        audio_path = await self._generate_voice(spoken_text, session, emo_switch)
        if not audio_path:
            return

        try:
            chain = MessageChain(chain=[Comp.Record.fromFileSystem(audio_path)])
            await event.send(chain)
        except Exception:
            logger.error("VocuTTS: failed to send voice message", exc_info=True)
        finally:
            try:
                os.remove(audio_path)
            except OSError:
                pass

    async def terminate(self) -> None:
        self.sessions.clear()
        if self._http and not self._http.closed:
            await self._http.close()
            self._http = None
