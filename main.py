from __future__ import annotations

import time

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .models import SESSION_EXPIRE_SECONDS, VOCUTTS_SKIP_FLAG, SessionTTSConfig
from .text_processor import process_text
from .vocu_client import VocuClient, try_remove_file


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
        self._client = VocuClient()

    async def initialize(self) -> None:
        await self._client.ensure_session()

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
        self._cleanup_stale_sessions()
        return session

    def _cleanup_stale_sessions(self) -> None:
        now = time.time()
        stale = [
            k
            for k, v in self.sessions.items()
            if now - v.last_active > SESSION_EXPIRE_SECONDS
        ]
        for k in stale:
            del self.sessions[k]

    def _resolve_voice_id(self, session: SessionTTSConfig) -> str:
        return session.voice_id or self._get_cfg("voice_id", "")

    def _resolve_prompt_id(self, session: SessionTTSConfig) -> str:
        return session.prompt_id or self._get_cfg("prompt_id", "default")

    def _resolve_preset(self, session: SessionTTSConfig) -> str:
        return session.preset or self._get_cfg("preset", "balance")

    # ── commands ──────────────────────────────────────────────

    @filter.command_group("vocutts")
    def vocutts_group(self, event: AstrMessageEvent) -> None:
        """VocuTTS 语音合成"""

    @vocutts_group.command("on")
    async def vocutts_on(self, event: AstrMessageEvent):
        """开启当前会话的 VocuTTS"""
        event.set_extra(VOCUTTS_SKIP_FLAG, True)
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
        event.set_extra(VOCUTTS_SKIP_FLAG, True)
        umo = event.unified_msg_origin
        session = self._get_session(umo)
        session.enabled = False
        yield event.plain_result("VocuTTS 已关闭。")

    @vocutts_group.command("status")
    async def vocutts_status(self, event: AstrMessageEvent):
        """查看当前会话 VocuTTS 状态"""
        event.set_extra(VOCUTTS_SKIP_FLAG, True)
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
    async def vocutts_voice(self, event: AstrMessageEvent, voice_id: str = ""):
        """设置当前会话的声音角色: /vocutts voice <voice_id>"""
        event.set_extra(VOCUTTS_SKIP_FLAG, True)
        umo = event.unified_msg_origin
        session = self._get_session(umo)

        if not voice_id:
            current = self._resolve_voice_id(session)
            yield event.plain_result(
                f"当前 Voice ID: {current or '未设置'}\n用法: /vocutts voice <voice_id>"
            )
            return

        session.voice_id = voice_id
        yield event.plain_result(f"已将当前会话的 Voice ID 设为: {voice_id}")

    @vocutts_group.command("voices")
    async def vocutts_voices(self, event: AstrMessageEvent):
        """列出所有可用的声音角色"""
        event.set_extra(VOCUTTS_SKIP_FLAG, True)
        api_key = self._get_cfg("api_key", "")
        if not api_key:
            yield event.plain_result("API Key 未配置，请在 WebUI 中设置。")
            return

        base_url = self._get_cfg("api_base_url", "https://v1.vocu.ai")
        voices, err = await self._client.list_voices(
            api_key=api_key, api_base_url=base_url
        )
        if voices is None:
            yield event.plain_result(err)
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
    async def vocutts_style(self, event: AstrMessageEvent, style_id: str = ""):
        """设置当前会话的声音风格: /vocutts style <prompt_id>"""
        event.set_extra(VOCUTTS_SKIP_FLAG, True)
        umo = event.unified_msg_origin
        session = self._get_session(umo)

        if not style_id:
            current = self._resolve_prompt_id(session)
            yield event.plain_result(
                f"当前 Style ID: {current}\n用法: /vocutts style <prompt_id>"
            )
            return

        session.prompt_id = style_id
        yield event.plain_result(f"已将当前会话的 Style ID 设为: {style_id}")

    @vocutts_group.command("preset")
    async def vocutts_preset(self, event: AstrMessageEvent, preset_name: str = ""):
        """设置生成预设: /vocutts preset <creative|balance|stable>"""
        event.set_extra(VOCUTTS_SKIP_FLAG, True)
        umo = event.unified_msg_origin
        session = self._get_session(umo)
        valid = {"creative", "balance", "stable"}

        if not preset_name or preset_name not in valid:
            current = self._resolve_preset(session)
            yield event.plain_result(
                f"当前预设: {current}\n"
                f"用法: /vocutts preset <{'|'.join(sorted(valid))}>"
            )
            return

        session.preset = preset_name
        yield event.plain_result(f"已将当前会话的预设设为: {preset_name}")

    # ── after_message_sent hook ──────────────────────────────

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """Intercept sent messages and follow up with TTS voice."""
        if event.get_extra(VOCUTTS_SKIP_FLAG, False):
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

        if any(isinstance(comp, Comp.Record) for comp in result.chain):
            return

        text_parts: list[str] = []
        for comp in result.chain:
            if isinstance(comp, Comp.Plain) and comp.text:
                text_parts.append(comp.text)

        full_text = "".join(text_parts).strip()
        if not full_text:
            return

        spoken_text, emo_switch = process_text(
            full_text,
            mode=self._get_cfg("bracket_mode", "strip"),
            emotion_keywords=self._get_cfg("emotion_keywords", ""),
        )
        if not spoken_text:
            return

        api_key = self._get_cfg("api_key", "")
        voice_id = self._resolve_voice_id(session)
        if not api_key or not voice_id:
            return

        audio_path = await self._client.generate_voice(
            spoken_text,
            api_key=api_key,
            voice_id=voice_id,
            prompt_id=self._resolve_prompt_id(session),
            preset=self._resolve_preset(session),
            api_base_url=self._get_cfg("api_base_url", "https://v1.vocu.ai"),
            break_clone=self._get_cfg("break_clone", True),
            language=self._get_cfg("language", "auto"),
            speech_rate=self._get_cfg("speech_rate", 1.0),
            vivid=self._get_cfg("vivid", False),
            flash=self._get_cfg("flash", False),
            emo_switch=emo_switch,
        )
        if not audio_path:
            return

        event.set_extra(VOCUTTS_SKIP_FLAG, True)
        try:
            chain = MessageChain(chain=[Comp.Record.fromFileSystem(audio_path)])
            await event.send(chain)
        except Exception:
            logger.error("VocuTTS: failed to send voice message", exc_info=True)
        finally:
            try_remove_file(audio_path)

    async def terminate(self) -> None:
        self.sessions.clear()
        await self._client.close()
