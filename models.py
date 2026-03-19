from __future__ import annotations

import time
from dataclasses import dataclass, field

import aiohttp

VOCUTTS_SKIP_FLAG = "_vocutts_skip_tts"
SESSION_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 days
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
AUDIO_HOST_ALLOWLIST = {"storage.vocu.ai", "storage.vocustatic.com", "v1.vocu.ai"}
MAX_TTS_TEXT_LENGTH = 5000
AUDIO_CONTENT_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/ogg",
    "binary/octet-stream",
    "application/octet-stream",
}


@dataclass
class SessionTTSConfig:
    """Per-session TTS state and overrides."""

    enabled: bool = False
    voice_id: str | None = None
    prompt_id: str | None = None
    preset: str | None = None
    last_active: float = field(default_factory=time.time)
