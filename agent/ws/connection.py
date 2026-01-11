import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional, Literal

from fastapi import WebSocket

from agents.realtime import RealtimeSession

from ..core.core_types import SessionState


@dataclass(slots=True)
class OutgoingMessage:
    kind: Literal["text", "bytes", "close"]
    data: Any
    code: Optional[int] = None
    reason: str = ""


@dataclass(slots=True)
class Connection:
    session_id: str
    websocket: WebSocket
    state: SessionState
    created_at: float
    outgoing: asyncio.Queue[OutgoingMessage]
    incoming_audio: asyncio.Queue[bytes]
    session_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session_context: Optional[Any] = None
    session: Optional[RealtimeSession] = None
    writer_task: Optional[asyncio.Task] = None
    event_task: Optional[asyncio.Task] = None
    audio_pump_task: Optional[asyncio.Task] = None
    tts_task: Optional[asyncio.Task] = None
    closed: bool = False
