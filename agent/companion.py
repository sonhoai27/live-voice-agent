import asyncio
from asyncio.log import logger
import json
import logging
import os
import time
from typing import Any

from .core.core_types import SessionState
from .core.dispatcher import EventDispatcher, EventSerializer
from .core.tts_service import TTSService
from dotenv import load_dotenv
from fastapi import WebSocket

load_dotenv()
logging.basicConfig(level=logging.INFO)

from agents.realtime import RealtimeRunner, RealtimeSession
from agents.realtime.config import RealtimeUserInputMessage, RealtimeRunConfig
from agents.realtime.model import RealtimeModelConfig
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

from cartesia_tts import CartesiaTTS

try:
    from .agent import get_starting_agent
except ImportError:
    from agent import get_starting_agent


class RealtimeWebSocketManager:
    def __init__(self):
        self.active_sessions: dict[str, RealtimeSession] = {}
        self.session_contexts: dict[str, Any] = {}
        self.websockets: dict[str, WebSocket] = {}
        self.audio_tasks: dict[str, asyncio.Task] = {}
        self.session_metrics: dict[str, dict] = {} # Track metrics per session (deprecated, use session_states)
        # Higher-level typed session state container (for refactor)
        self.session_states: dict[str, "SessionState"] = {}
        # Event dispatcher
        self.dispatcher = EventDispatcher(self)
        # TTS service (decoupled from manager)
        self.tts_service = TTSService(cartesia_tts=CartesiaTTS(api_key=os.getenv("CARTESIA_API_KEY")))  # Start with no TTS; enable later as needed


    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        self.websockets[session_id] = websocket

        agent = get_starting_agent()
        # runner = RealtimeRunner(agent)
        # If you want to customize the runner behavior, you can pass options:
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
                    "prompt": "Always transcribe the output into English"
                },
                "output_modalities": ['text'],
            },
            "api_key": os.getenv("AZURE_OPENAI_API_KEY"),
            "url": os.getenv("AZURE_OPENAI_REALTIME_URL"),
        }
        session_context = await runner.run(model_config=model_config)
        session = await session_context.__aenter__()
        self.active_sessions[session_id] = session
        self.session_contexts[session_id] = session_context

        # Initialize typed session state and keep compatibility alias for existing metrics
        state = SessionState(session_id=session_id)
        self.session_states[session_id] = state
        self.session_metrics[session_id] = state.metrics

        # Start event processing task
        asyncio.create_task(self._process_events(session_id))

    async def disconnect(self, session_id: str):
        if session_id in self.session_contexts:
            await self.session_contexts[session_id].__aexit__(None, None, None)
            del self.session_contexts[session_id]
        if session_id in self.active_sessions:
            del self.active_sessions[session_id]
        if session_id in self.websockets:
            del self.websockets[session_id]
        if session_id in self.session_states:
            del self.session_states[session_id]
        if session_id in self.session_metrics:
            del self.session_metrics[session_id]

    async def send_audio(self, session_id: str, audio_bytes: bytes):
        if session_id in self.active_sessions:
            await self.active_sessions[session_id].send_audio(audio_bytes)

    async def send_client_event(self, session_id: str, event: dict[str, Any]):
        """Send a raw client event to the underlying realtime model."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
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
        session = self.active_sessions.get(session_id)
        if not session:
            return
        await session.send_message(message)  # delegates to RealtimeModelSendUserInput path

    async def interrupt(self, session_id: str) -> None:
        """Interrupt current model playback/response for a session."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        await session.interrupt()

    async def _process_events(self, session_id: str):
        try:
            session = self.active_sessions[session_id]
            websocket = self.websockets[session_id]
            state = self.session_states[session_id]

            async for event in session:
                # 1. Serialize event (pure, no side-effects)
                serialized_event = EventSerializer.serialize(event)
                
                # 2. Dispatch to lifecycle handlers (updates state)
                await self.dispatcher.dispatch(session_id, serialized_event, websocket)
                # 3. Emit session-level metrics for certain lifecycle events
                try:
                    evt_type = serialized_event.get("type")
                    now = time.time()
                    if evt_type == "response.created":
                        await self._send_metrics(session_id, now, "response_created")
                    elif evt_type in ("response.text.delta", "response.audio.delta"):
                        await self._send_metrics(session_id, now, "llm_start")
                except Exception:
                    pass

                # 3. TTS handling (special case: trigger audio generation)
                if self.tts_service and serialized_event.get("type") == "response.output_text.done":
                    # Prefer existing session transcript, fall back to event payload transcript
                    transcript = state.last_transcript or serialized_event.get("transcript")
                    print(f"Dispatched event for session {session_id}: {serialized_event}")
                    if transcript:
                        # ensure session stores latest transcript
                        state.last_transcript = transcript

                        # Cancel any existing audio task
                        if session_id in self.audio_tasks:
                            self.audio_tasks[session_id].cancel()

                        # Spawn background task for TTS streaming
                        task = asyncio.create_task(
                            self._stream_response(session_id, transcript, websocket)
                        )
                        state.audio_task = task
                        self.audio_tasks[session_id] = task
                        task.add_done_callback(lambda t: self.audio_tasks.pop(session_id, None))
                
        except Exception as e:
            logger.error(f"Error processing events for session {session_id}: {e}")

    async def _send_metrics(self, session_id: str, currenttime: float, metric_type: str):
        """Helper to calculate and send metrics."""
        if session_id not in self.session_metrics:
             return
        
        metrics = self.session_metrics[session_id]
        websocket = self.websockets.get(session_id)
        
        data = {}

        # Turn Latency: Speech End -> First Audio Sent
        if metric_type == "first_audio":
            if metrics.get("speech_end_time"):
                turn_latency = (currenttime - metrics["speech_end_time"]) * 1000
                data["turn"] = round(turn_latency, 2)

            # TTS Latency: Transcript/Audio Start -> First Audio Sent
            if metrics.get("tts_ready_time"):
                tts_latency = (currenttime - metrics["tts_ready_time"]) * 1000
                data["tts"] = round(tts_latency, 2)
        
        # LLM Latency: Response Created -> Text/Audio Delta
        if metric_type == "llm_start":
             # We rely on previous speech_end
             pass
             
        # STT approximation: Speech End -> Response Created (processing time)
        if metric_type == "response_created":
            if metrics.get("speech_end_time"):
                stt_latency = (currenttime - metrics["speech_end_time"]) * 1000
                data["stt"] = round(stt_latency, 2)

        # Note: TTS streaming is handled by `RealtimeWebSocketManager._stream_response`
        # which delegates to the decoupled `TTSService`. See `_stream_response` below.
        # Send metrics if any were collected
        if data and websocket:
            try:
                await websocket.send_text(json.dumps({"type": "metrics", "data": data}))
            except Exception as e:
                logger.error(f"Failed to send metrics for session {session_id}: {e}")

    async def _stream_response(self, session_id: str, transcript: str, websocket: WebSocket) -> None:
        """Spawned background task to stream TTS audio for a session.

        Delegates generation/streaming to `self.tts_service.stream_to_websocket` and
        updates session metrics/state appropriately.
        """
        start = time.time()
        state = self.session_states.get(session_id)
        if not state:
            return

        # Mark TTS ready time for metrics (consistent with dispatcher)
        state.metrics["tts_ready_time"] = time.time()

        async def _on_first_chunk():
            # When the first audio chunk is sent, emit metrics
            try:
                await self._send_metrics(session_id, time.time(), "first_audio")
            except Exception:
                pass

        try:
            await self.tts_service.stream_to_websocket(session_id, transcript, websocket, on_first_chunk_callback=_on_first_chunk)
        except asyncio.CancelledError:
            logger.info(f"TTS streaming cancelled for session {session_id}")
            raise
        except Exception as e:
            logger.error(f"Error during TTS streaming for session {session_id}: {e}")
        finally:
            elapsed = (time.time() - start) * 1000
            logger.info(f"_stream_response completed in {elapsed:.2f} ms for session {session_id}")
            
manager = RealtimeWebSocketManager()
