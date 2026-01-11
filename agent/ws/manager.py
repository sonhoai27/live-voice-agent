import asyncio
import json
import logging
import os
import sys
import time
from array import array
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import WebSocket

from agents.realtime import RealtimeRunner, RealtimeSession
from agents.realtime.config import RealtimeUserInputMessage, RealtimeRunConfig
from agents.realtime.model import RealtimeModelConfig
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

from ..cartesia_tts import CartesiaTTS

from .connection import Connection, OutgoingMessage
from ..core.core_types import SessionState
from ..core.dispatcher import EventDispatcher, EventSerializer
from ..core.tts_service import TTSService

try:
    from ..agent import get_starting_agent
except ImportError:
    from agent import get_starting_agent

load_dotenv()
logging.basicConfig(level=logging.INFO)


class RealtimeWebSocketManager:
    """Owns per-session connections, queues, and model lifecycle."""
    def __init__(self):
        self.connections: dict[str, Connection] = {}
        self.session_states: dict[str, SessionState] = {}
        # Compatibility: used by dispatcher interruption handler
        self.audio_tasks: dict[str, asyncio.Task] = {}
        # Event dispatcher
        self.dispatcher = EventDispatcher(self)
        # TTS service (decoupled from manager)
        self.tts_service = TTSService(cartesia_tts=CartesiaTTS(api_key=os.getenv("CARTESIA_API_KEY")))
        self._outgoing_max = int(os.getenv("WS_OUTGOING_MAX", "512"))
        self._incoming_audio_max = int(os.getenv("WS_INCOMING_AUDIO_MAX", "32"))
        self._tts_chunk_bytes = int(os.getenv("WS_TTS_CHUNK_BYTES", "4096"))
        self._logger = logging.getLogger(__name__)

    def _get_conn(self, session_id: str) -> Optional[Connection]:
        return self.connections.get(session_id)

    async def _cancel_task(self, task: Optional[asyncio.Task]) -> None:
        """Best-effort task cancellation to avoid leaking background workers."""
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def send_json(self, session_id: str, payload: dict[str, Any], *, drop_if_full: bool = False) -> bool:
        conn = self._get_conn(session_id)
        if not conn or conn.closed:
            return False
        if conn.writer_task is None:
            conn.writer_task = asyncio.create_task(self._writer(conn))
        # Drop best-effort events when the client is slow to avoid blocking the loop.
        msg = OutgoingMessage(kind="text", data=json.dumps(payload))
        if drop_if_full:
            try:
                conn.outgoing.put_nowait(msg)
                return True
            except asyncio.QueueFull:
                return False
        await conn.outgoing.put(msg)
        return True

    async def send_bytes(self, session_id: str, data: bytes) -> bool:
        conn = self._get_conn(session_id)
        if not conn or conn.closed:
            return False
        if conn.writer_task is None:
            conn.writer_task = asyncio.create_task(self._writer(conn))
        await conn.outgoing.put(OutgoingMessage(kind="bytes", data=data))
        return True

    async def _writer(self, conn: Connection) -> None:
        try:
            while True:
                # Single-writer invariant: all WS sends flow through this task.
                msg = await conn.outgoing.get()
                if msg.kind == "text":
                    await conn.websocket.send_text(msg.data)
                elif msg.kind == "bytes":
                    await conn.websocket.send_bytes(msg.data)
                elif msg.kind == "close":
                    await conn.websocket.close(code=msg.code or 1000, reason=msg.reason or "")
                    return
        except asyncio.CancelledError:
            return
        except Exception as e:
            self._logger.info("writer stopped for %s: %s", conn.session_id, e)
        finally:
            if not conn.closed:
                asyncio.create_task(self.disconnect(conn.session_id))

    async def _ensure_session(self, conn: Connection) -> RealtimeSession:
        """Create or return the realtime session for a connection (lazy init)."""
        async with conn.session_lock:
            if conn.session:
                return conn.session
            # Lazy-init to keep idle rooms lightweight.
            agent = get_starting_agent()
            runner_config = RealtimeRunConfig(async_tool_calls=False)
            runner = RealtimeRunner(agent, config=runner_config)
            model_config: RealtimeModelConfig = {
                "initial_model_settings": {
                    "turn_detection": {
                        "type": "server_vad",
                        "prefix_padding_ms": 1000,
                        "silence_duration_ms": 1000,
                        "interrupt_response": True,
                        "create_response": True,
                    },
                    "input_audio_transcription": {
                        "model": "gpt-4o-transcribe",
                        "prompt": "Always transcribe the output into English",
                    },
                    "output_modalities": ["text"],
                },
                "api_key": os.getenv("AZURE_OPENAI_API_KEY"),
                "url": os.getenv("AZURE_OPENAI_REALTIME_URL"),
            }
            conn.session_context = await runner.run(model_config=model_config)
            conn.session = await conn.session_context.__aenter__()
            conn.event_task = asyncio.create_task(self._process_events(conn.session_id))
            return conn.session

    async def connect(self, websocket: WebSocket, session_id: str):
        """Register a WebSocket and initialize per-session state/queues."""
        await websocket.accept()

        if session_id in self.connections:
            await self.disconnect(session_id, code=1012, reason="replaced")

        state = SessionState(session_id=session_id)
        self.session_states[session_id] = state

        conn = Connection(
            session_id=session_id,
            websocket=websocket,
            state=state,
            created_at=time.time(),
            outgoing=asyncio.Queue(maxsize=self._outgoing_max),
            incoming_audio=asyncio.Queue(maxsize=self._incoming_audio_max),
        )
        self.connections[session_id] = conn
        # writer task is lazy-started on first outbound message

    async def disconnect(self, session_id: str, *, code: int = 1000, reason: str = ""):
        """Tear down session tasks, close WS, and release resources."""
        conn = self.connections.pop(session_id, None)
        state = self.session_states.pop(session_id, None)
        if conn:
            conn.closed = True
            await self._cancel_task(conn.tts_task)
            await self._cancel_task(conn.audio_pump_task)
            await self._cancel_task(conn.event_task)
            await self._cancel_task(conn.writer_task)
            if conn.session_context:
                try:
                    await conn.session_context.__aexit__(None, None, None)
                except Exception:
                    pass
            try:
                await conn.websocket.close(code=code, reason=reason)
            except Exception:
                pass
        self.audio_tasks.pop(session_id, None)
        if state:
            state.connected = False

    async def send_audio(self, session_id: str, audio_bytes: bytes):
        """Queue inbound audio for the model pump."""
        conn = self._get_conn(session_id)
        if not conn or conn.closed:
            return
        if conn.audio_pump_task is None:
            conn.audio_pump_task = asyncio.create_task(self._audio_pump(conn.session_id))
        # Inbound audio is buffered to apply backpressure to the client.
        await conn.incoming_audio.put(audio_bytes)

    async def cancel_tts(self, session_id: str) -> None:
        """Stop any in-flight TTS stream for the session."""
        conn = self._get_conn(session_id)
        if not conn or conn.closed:
            return
        if conn.tts_task:
            conn.tts_task.cancel()
            conn.tts_task = None
        self.audio_tasks.pop(session_id, None)

    async def send_client_event(self, session_id: str, event: dict[str, Any]):
        """Send a raw client event to the underlying realtime model."""
        conn = self._get_conn(session_id)
        if not conn or conn.closed:
            return
        session = await self._ensure_session(conn)
        await session.model.send_event(
            RealtimeModelSendRawMessage(
                message={
                    "type": event["type"],
                    "other_data": {k: v for k, v in event.items() if k != "type"},
                }
            )
        )

    async def send_user_message(self, session_id: str, message: RealtimeUserInputMessage):
        """Send a structured user message via the higher-level API (supports input_image)."""
        conn = self._get_conn(session_id)
        if not conn or conn.closed:
            return
        session = await self._ensure_session(conn)
        await session.send_message(message)

    async def interrupt(self, session_id: str) -> None:
        """Interrupt current model playback/response for a session."""
        conn = self._get_conn(session_id)
        if not conn or conn.closed or not conn.session:
            return
        await conn.session.interrupt()

    async def _audio_pump(self, session_id: str) -> None:
        """Drain queued audio into the realtime session."""
        conn = self._get_conn(session_id)
        if not conn:
            return
        try:
            while True:
                audio_bytes = await conn.incoming_audio.get()
                session = await self._ensure_session(conn)
                await session.send_audio(audio_bytes)
        except asyncio.CancelledError:
            return
        except Exception as e:
            self._logger.info("audio_pump stopped for %s: %s", session_id, e)
        finally:
            if not conn.closed:
                asyncio.create_task(self.disconnect(session_id))

    async def _process_events(self, session_id: str):
        """Read model events, update state, and enqueue outbound messages."""
        conn = self._get_conn(session_id)
        if not conn or conn.closed or not conn.session:
            return
        try:
            session = conn.session
            state = conn.state

            async for event in session:
                # Serialize event (pure, no side-effects)
                serialized_event = EventSerializer.serialize(event)

                # Dispatch to lifecycle handlers (updates state)
                extra_msgs = await self.dispatcher.dispatch(session_id, serialized_event)

                evt_type = serialized_event.get("type")
                # Drop chatty events first if outbound queue backs up.
                drop_base = evt_type in {
                    "response.text.delta",
                    "response.audio.delta",
                    "history_updated",
                    "history_added",
                }
                await self.send_json(session_id, serialized_event, drop_if_full=drop_base)
                for extra in extra_msgs:
                    await self.send_json(session_id, extra, drop_if_full=True)

                # TTS handling (special case: trigger audio generation)
                if self.tts_service and serialized_event.get("type") == "response.output_text.done":
                    transcript = state.last_transcript or serialized_event.get("transcript")
                    if transcript:
                        state.last_transcript = transcript
                        await self.cancel_tts(session_id)
                        task = asyncio.create_task(self._stream_response(session_id, transcript))
                        state.audio_task = task
                        conn.tts_task = task
                        self.audio_tasks[session_id] = task
                        task.add_done_callback(lambda t: self.audio_tasks.pop(session_id, None))

        except Exception as e:
            self._logger.error("Error processing events for session %s: %s", session_id, e)
        finally:
            if not conn.closed:
                asyncio.create_task(self.disconnect(session_id))

    async def _stream_response(self, session_id: str, transcript: str) -> None:
        """Stream TTS audio chunks via the outbound writer queue."""
        start = time.time()
        state = self.session_states.get(session_id)
        if not state:
            return

        # Mark TTS ready time for metrics (consistent with dispatcher)
        state.metrics["tts_ready_time"] = time.time()

        try:
            await self.send_json(session_id, {"type": "audio_start"}, drop_if_full=False)
            buffer = bytearray()
            first_chunk_sent = False

            async for audio_chunk in self.tts_service.stream_audio(transcript):
                if not audio_chunk:
                    continue
                buffer.extend(audio_chunk)
                while len(buffer) >= self._tts_chunk_bytes:
                    chunk_to_send = bytes(buffer[: self._tts_chunk_bytes])
                    del buffer[: self._tts_chunk_bytes]
                    await self.send_bytes(session_id, chunk_to_send)
                    if not first_chunk_sent:
                        first_chunk_sent = True
                        now = time.time()
                        data: dict[str, Any] = {}
                        speech_end = state.metrics.get("speech_end_time")
                        if speech_end:
                            data["turn"] = round((now - speech_end) * 1000, 2)
                        tts_ready = state.metrics.get("tts_ready_time")
                        if tts_ready:
                            data["tts"] = round((now - tts_ready) * 1000, 2)
                        if data:
                            await self.send_json(session_id, {"type": "metrics", "data": data}, drop_if_full=True)

            if buffer:
                await self.send_bytes(session_id, bytes(buffer))
        except asyncio.CancelledError:
            self._logger.info("TTS streaming cancelled for session %s", session_id)
            raise
        except Exception as e:
            self._logger.error("Error during TTS streaming for session %s: %s", session_id, e)
        finally:
            elapsed = (time.time() - start) * 1000
            self._logger.info("_stream_response completed in %.2f ms for session %s", elapsed, session_id)

    def parse_json_int16_audio(self, samples: list[int]) -> bytes:
        """Convert JSON int16 arrays into PCM bytes without per-sample packing."""
        pcm = array("h", samples)
        if sys.byteorder != "little":
            pcm.byteswap()
        return pcm.tobytes()


manager = RealtimeWebSocketManager()
