from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .models import (
    AUDIO_CONTENT_TYPES,
    AUDIO_HOST_ALLOWLIST,
    DOWNLOAD_TIMEOUT,
    MAX_DOWNLOAD_BYTES,
    MAX_TTS_TEXT_LENGTH,
)


class VocuClient:
    """Async client wrapping the Vocu TTS API."""

    def __init__(self) -> None:
        self._http: aiohttp.ClientSession | None = None

    async def ensure_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()
            self._http = None

    def _build_audio_host_allowlist(self, api_base_url: str) -> set[str]:
        parsed = urlparse(api_base_url)
        hosts = set(AUDIO_HOST_ALLOWLIST)
        if parsed.hostname:
            hosts.add(parsed.hostname)
        return hosts

    async def generate_voice(
        self,
        text: str,
        *,
        api_key: str,
        voice_id: str,
        prompt_id: str,
        preset: str,
        api_base_url: str = "https://v1.vocu.ai",
        break_clone: bool = True,
        language: str = "auto",
        speech_rate: float = 1.0,
        vivid: bool = False,
        flash: bool = False,
        emo_switch: list[int] | None = None,
    ) -> str | None:
        """Call Vocu synchronous TTS API. Returns local file path or None."""
        base_url = api_base_url.rstrip("/")

        if len(text) > MAX_TTS_TEXT_LENGTH:
            text = text[:MAX_TTS_TEXT_LENGTH]

        payload: dict = {
            "voiceId": voice_id,
            "text": text,
            "promptId": prompt_id,
            "preset": preset,
            "break_clone": break_clone,
            "language": language,
            "speechRate": speech_rate,
            "vivid": vivid,
            "flash": flash,
        }
        if emo_switch:
            payload["emo_switch"] = emo_switch

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            http = await self.ensure_session()
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

            return await self._download_audio(audio_url, api_base_url)
        except Exception:
            logger.error("VocuTTS: voice generation failed", exc_info=True)
            return None

    async def _download_audio(self, url: str, api_base_url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            logger.error(f"VocuTTS: refusing non-HTTP audio URL: {url}")
            return None

        if not parsed.hostname:
            logger.error(f"VocuTTS: audio URL has no hostname: {url}")
            return None

        allowed_hosts = self._build_audio_host_allowlist(api_base_url)
        if parsed.hostname not in allowed_hosts:
            logger.error(
                f"VocuTTS: audio host '{parsed.hostname}' not in allowlist {allowed_hosts}"
            )
            return None

        temp_dir = get_astrbot_temp_path()
        os.makedirs(temp_dir, exist_ok=True)
        path = os.path.join(temp_dir, f"vocutts_{uuid.uuid4()}.mp3")

        try:
            http = await self.ensure_session()
            async with http.get(
                url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    logger.error(f"VocuTTS: audio download failed: {resp.status}")
                    return None

                content_type = resp.content_type or ""
                if content_type and content_type not in AUDIO_CONTENT_TYPES:
                    logger.error(
                        f"VocuTTS: unexpected Content-Type '{content_type}', expected audio"
                    )
                    return None

                downloaded = 0
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        downloaded += len(chunk)
                        if downloaded > MAX_DOWNLOAD_BYTES:
                            logger.error(
                                f"VocuTTS: audio exceeds {MAX_DOWNLOAD_BYTES} bytes limit, aborted"
                            )
                            break
                        f.write(chunk)

                if downloaded > MAX_DOWNLOAD_BYTES:
                    try_remove_file(path)
                    return None

            return path
        except Exception:
            logger.error("VocuTTS: audio download failed", exc_info=True)
            try_remove_file(path)
            return None

    async def list_voices(
        self, *, api_key: str, api_base_url: str = "https://v1.vocu.ai"
    ) -> tuple[list[dict] | None, str]:
        """Returns (voice_list, error_message). error_message is empty on success."""
        base_url = api_base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            http = await self.ensure_session()
            async with http.get(
                f"{base_url}/api/voice",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (401, 403):
                    return None, "API Key 认证失败，请检查 Key 是否正确。"
                if resp.status != 200:
                    return None, f"Vocu API 返回错误 (HTTP {resp.status})。"
                data = await resp.json()
                return data.get("data", []), ""
        except aiohttp.ClientError:
            return None, "网络连接失败，请检查网络或 API 地址配置。"
        except Exception:
            logger.error("VocuTTS: list voices failed", exc_info=True)
            return None, "获取声音列表时发生未知错误。"


def try_remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
