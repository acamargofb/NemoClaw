"""Session state management for voice conversations."""
import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SESSION_TTL = 7200  # 2 hours in seconds


@dataclass
class VoiceSession:
    chat_id: str
    session_id: str
    voice_pt_path: Optional[str] = None   # PersonaPlex .pt embedding path
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def touch(self):
        self.last_active = time.time()

    @property
    def expired(self) -> bool:
        return (time.time() - self.last_active) > SESSION_TTL

    @property
    def elapsed(self) -> int:
        return int(time.time() - self.created_at)


class SessionStore:
    def __init__(self, voice_samples_dir: str):
        self._sessions: dict[str, VoiceSession] = {}
        self._lock = asyncio.Lock()
        self.voice_samples_dir = Path(voice_samples_dir)

    def _voice_pt_path(self, chat_id: str) -> Optional[str]:
        for ext in (".wav", ".pt"):
            p = self.voice_samples_dir / f"{chat_id}{ext}"
            if p.exists():
                return str(p)
        return None

    async def get_or_create(self, chat_id: str) -> VoiceSession:
        async with self._lock:
            session = self._sessions.get(chat_id)
            if session is None or session.expired:
                session = VoiceSession(
                    chat_id=chat_id,
                    session_id=f"{chat_id}-{int(time.time())}",
                    voice_pt_path=self._voice_pt_path(chat_id),
                )
                self._sessions[chat_id] = session
            else:
                session.touch()
                # Refresh voice path in case enrollment happened since session start
                if session.voice_pt_path is None:
                    session.voice_pt_path = self._voice_pt_path(chat_id)
            return session

    async def cleanup(self):
        async with self._lock:
            expired = [k for k, v in self._sessions.items() if v.expired]
            for k in expired:
                del self._sessions[k]
