import struct
import logging
from dataclasses import dataclass, field
from typing import Optional, Any
from enum import Enum, auto

logger = logging.getLogger(__name__)


class AgentState(Enum):
    INITIALIZING = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    ERROR = auto()


class UserState(Enum):
    SPEAKING = auto()
    LISTENING = auto()
    ERROR = auto()


@dataclass
class UserTurn:
    speech_start_time: Optional[float] = None
    speech_end_time: Optional[float] = None
    commit_time: Optional[float] = None
    transcript: Optional[str] = None
    status: str = "idle"


@dataclass
class AgentTurn:
    agent_name: Optional[str] = None
    think_start_time: Optional[float] = None
    speak_start_time: Optional[float] = None
    speak_end_time: Optional[float] = None
    status: str = "idle"


@dataclass
class SessionState:
    session_id: str
    connected: bool = True
    total_cost: float = 0.0
    history: list = field(default_factory=list)
    current_user_turn: Optional[UserTurn] = None
    current_agent_turn: Optional[AgentTurn] = None
    audio_task: Optional[Any] = None
    tts_enabled: bool = False
    last_transcript: Optional[str] = None
    metrics: dict = field(default_factory=dict)
    metrics_sent_flags: dict = field(default_factory=dict)