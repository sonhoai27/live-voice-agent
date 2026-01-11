import json
import time
import logging
from typing import Any, Optional
from fastapi import WebSocket
from typing_extensions import assert_never

from agents.realtime.items import RealtimeItem
from agents.realtime import RealtimeSessionEvent

from .core_types import SessionState, AgentTurn, UserTurn

logger = logging.getLogger(__name__)


class EventSerializer:
    """Pure serializer: converts RealtimeSessionEvent → normalized dict."""

    @staticmethod
    def unwrap_data(x):
        while True:
            if x is None:
                return None
            if isinstance(x, dict):
                return x
            if hasattr(x, "data"):
                x = getattr(x, "data")
                continue
            return None

    @staticmethod
    def sanitize_history_item(item: RealtimeItem) -> dict[str, Any]:
        item_dict = item.model_dump()
        content = item_dict.get("content")
        if isinstance(content, list):
            sanitized_content: list[Any] = []
            for part in content:
                if isinstance(part, dict):
                    sanitized_part = part.copy()
                    if sanitized_part.get("type") in {"audio", "input_audio"}:
                        sanitized_part.pop("audio", None)
                    sanitized_content.append(sanitized_part)
                else:
                    sanitized_content.append(part)
            item_dict["content"] = sanitized_content
        return item_dict

    @staticmethod
    def serialize(event: RealtimeSessionEvent) -> dict[str, Any]:
        base_event: dict[str, Any] = {"type": event.type}
        # lightweight mapping, keep parity with previous implementation
        if event.type == "agent_start":
            base_event["agent"] = event.agent.name
        elif event.type == "agent_end":
            base_event["agent"] = event.agent.name
        elif event.type == "handoff":
            base_event["from"] = event.from_agent.name
            base_event["to"] = event.to_agent.name
        elif event.type == "tool_start":
            base_event["tool"] = event.tool.name
        elif event.type == "tool_end":
            base_event["tool"] = event.tool.name
            base_event["output"] = str(event.output)
        elif event.type == "audio":
            import base64

            base_event["audio"] = base64.b64encode(event.audio.data).decode("utf-8")
        elif event.type == "history_updated":
            base_event["history"] = [EventSerializer.sanitize_history_item(item) for item in event.history]
        elif event.type == "history_added":
            try:
                base_event["item"] = EventSerializer.sanitize_history_item(event.item)
            except Exception:
                base_event["item"] = None
        elif event.type == "guardrail_tripped":
            base_event["guardrail_results"] = [{"name": result.guardrail.name} for result in event.guardrail_results]
        elif event.type == "raw_model_event":
            raw = event.data
            base_event["raw_model_event"] = {"type": raw.type}
            if getattr(raw, "type", None) == "raw_server_event":
                payload = EventSerializer.unwrap_data(getattr(raw, "data", None))
                if payload and payload.get("type") == "response.output_text.done":
                    base_event["type"] = "response.output_text.done"
                    base_event["transcript"] = payload.get("text", "")
                else:
                    base_event["type"] = payload.get("type")
                    if "response" in payload:
                        base_event["response"] = payload["response"]
                        
            data = getattr(raw, "data", None) or getattr(raw, "message", None) or raw
            if isinstance(data, dict):
                if "transcript" in data:
                    base_event["raw_model_event"]["transcript"] = data["transcript"]
                item = data.get("item") or data.get("conversation_item")
                if isinstance(item, dict):
                    content = item.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and "transcript" in part:
                                base_event["raw_model_event"]["transcript"] = part["transcript"]
                                break
        elif event.type == "error":
            base_event["error"] = str(event.error) if hasattr(event, "error") else "Unknown error"
        return base_event


class EventDispatcher:
    """Stateful dispatcher that handles lifecycle hooks and event routing."""

    def __init__(self, manager: Any):
        self.manager = manager

    @staticmethod
    def standardize_event_payload(event: dict, participant_id: str = None, state: str = None, data: dict = None) -> dict:
        payload = {"type": event.get("type"), "timestamp": event.get("timestamp", time.time())}
        if participant_id:
            payload["participant_id"] = participant_id
        if state:
            payload["state"] = state
        if data:
            payload["data"] = data
        for k, v in event.items():
            if k not in payload:
                payload[k] = v
        return payload

    # Lifecycle hooks
    async def on_enter(self, session_id: str):
        logger.info(f"[Lifecycle] on_enter: Agent entered session {session_id}")
        if hasattr(self, "on_enter_hook") and callable(self.on_enter_hook):
            await self.on_enter_hook(session_id)

    async def on_exit(self, session_id: str):
        logger.info(f"[Lifecycle] on_exit: Agent exiting session {session_id}")
        if hasattr(self, "on_exit_hook") and callable(self.on_exit_hook):
            await self.on_exit_hook(session_id)
        state = self.manager.session_states.get(session_id)
        if state:
            state.current_agent_turn = None
            state.current_user_turn = None

    async def on_user_turn_completed(self, session_id: str):
        logger.info(f"[Lifecycle] on_user_turn_completed: User turn completed in session {session_id}")
        if hasattr(self, "on_user_turn_completed_hook") and callable(self.on_user_turn_completed_hook):
            await self.on_user_turn_completed_hook(session_id)

    async def dispatch(self, session_id: str, serialized_event: dict[str, Any], websocket: WebSocket):
        state = self.manager.session_states.get(session_id)
        if not state:
            return
        event_type = serialized_event.get("type")
        current_time = time.time()
        await self._handle_room_event(state, serialized_event, current_time)
        await self._handle_agent_event(state, serialized_event, current_time)
        await self._handle_user_event(state, serialized_event, current_time)
        # Extract transcripts from events (STT/LLM) and store on state and current user turn
        await self._handle_transcript_event(state, serialized_event, current_time)
        await self._handle_conversation_event(state, serialized_event, current_time, websocket, session_id)
        await self._handle_history_event(state, serialized_event)
        await self._handle_audio_event(state, serialized_event, current_time)
        await self._handle_interruption(state, serialized_event, session_id, websocket)
        await self._handle_metrics_event(state, serialized_event, current_time, websocket)
        await websocket.send_text(json.dumps(serialized_event))

    async def _handle_room_event(self, state: SessionState, event: dict, current_time: float):
        pass

    async def _handle_agent_event(self, state: SessionState, event: dict, current_time: float):
        event_type = event.get("type")
        if event_type == "agent_start":
            agent_name = event.get("agent")
            state.current_agent_turn = AgentTurn(agent_name=agent_name, think_start_time=current_time)
            logger.info(f"Agent started: {agent_name}")
        elif event_type == "agent_end":
            if state.current_agent_turn:
                state.current_agent_turn.status = "done"
                state.current_agent_turn.speak_end_time = current_time
            logger.info(f"Agent ended: {event.get('agent')}")
        elif event_type == "handoff":
            if state.current_agent_turn:
                state.current_agent_turn.status = "done"
            state.current_agent_turn = AgentTurn(agent_name=event.get("to"), think_start_time=current_time)
            logger.info(f"Handoff from {event.get('from')} to {event.get('to')}")

    async def _handle_user_event(self, state: SessionState, event: dict, current_time: float):
        event_type = event.get("type")
        if event_type == "input_audio_buffer.speech_started":
            if not state.current_user_turn:
                state.current_user_turn = UserTurn()
            state.current_user_turn.speech_start_time = current_time
            state.current_user_turn.status = "listening"
            logger.debug(f"User speech started")
        elif event_type == "input_audio_buffer.speech_stopped":
            if state.current_user_turn:
                state.current_user_turn.speech_end_time = current_time
                # record speech end into metrics for latency calculations
                try:
                    state.metrics["speech_end_time"] = current_time
                except Exception:
                    pass
                state.current_user_turn.status = "stopped"
            logger.debug(f"User speech stopped")
        elif event_type == "input_audio_buffer.committed":
            if state.current_user_turn:
                state.current_user_turn.commit_time = current_time
                state.current_user_turn.status = "committed"
            logger.debug(f"User audio committed")

    async def _handle_conversation_event(self, state: SessionState, event: dict, current_time: float, websocket: WebSocket, session_id: str):
        event_type = event.get("type")
        logger.debug(f"[_handle_conversation_event] event_type={event_type}")
        if event_type == "response.created":
            # Ensure we have an AgentTurn to record think_start_time
            if not state.current_agent_turn:
                state.current_agent_turn = AgentTurn(think_start_time=current_time)
            else:
                state.current_agent_turn.think_start_time = current_time
            state.current_agent_turn.status = "thinking"
        elif event_type in ("response.text.delta", "response.audio.delta"):
            # If no agent turn exists, create one so we can record speak_start_time
            if not state.current_agent_turn:
                state.current_agent_turn = AgentTurn(speak_start_time=current_time)
                state.current_agent_turn.status = "speaking"
            elif state.current_agent_turn.status == "thinking":
                state.current_agent_turn.speak_start_time = current_time
                state.current_agent_turn.status = "speaking"
        elif event_type == "response.output_text.done":
            transcript = event.get("transcript")
            if transcript:
                state.last_transcript = transcript
        elif event_type == "response.done":
            if state.current_agent_turn:
                state.current_agent_turn.speak_end_time = current_time
                state.current_agent_turn.status = "done"
            # Attempt to extract usage; if missing, set zeros so metrics still report
            resp = event.get("response") or {}
            usage = resp.get("usage") if isinstance(resp, dict) else None
            if usage and isinstance(usage, dict):
                input_tokens = usage.get("input_tokens", 0) or 0
                output_tokens = usage.get("output_tokens", 0) or 0
            else:
                input_tokens = 0
                output_tokens = 0
                logger.warning("⚠️  response.done không có usage field; defaulting tokens to 0")
            cost = (input_tokens * 0.000004) + (output_tokens * 0.000016)
            state.total_cost += cost
            state.metrics["input_tokens"] = input_tokens
            state.metrics["output_tokens"] = output_tokens
            logger.info(f"✅ Usage: In={input_tokens}, Out={output_tokens}, Cost=${cost:.6f}, Total=${state.total_cost:.4f}")

    async def _handle_history_event(self, state: SessionState, event: dict):
        event_type = event.get("type")
        if event_type == "history_added":
            if "item" in event and event["item"]:
                state.history.append(event["item"])
        elif event_type == "history_updated":
            if "history" in event:
                state.history = event["history"]

    async def _handle_audio_event(self, state: SessionState, event: dict, current_time: float):
        event_type = event.get("type")
        if event_type == "response.output_text.done" and state.last_transcript:
            state.metrics["tts_ready_time"] = current_time

    async def _handle_transcript_event(self, state: SessionState, event: dict, current_time: float):
        """Handle transcripts coming from model or STT and attach to user turn/state."""
        # Top-level transcript (e.g., response.output_text.done)
        transcript = None
        if isinstance(event, dict):
            transcript = event.get("transcript")
            # check raw_model_event container
            raw = event.get("raw_model_event")
            if not transcript and isinstance(raw, dict):
                transcript = raw.get("transcript")

        if transcript:
            # store as last transcript for session
            state.last_transcript = transcript
            # attach to current user turn if present
            if state.current_user_turn:
                try:
                    state.current_user_turn.transcript = transcript
                except Exception:
                    pass

    async def _handle_interruption(self, state: SessionState, event: dict, session_id: str, websocket: WebSocket):
        event_type = event.get("type")
        if event_type == "input_audio_buffer.speech_started":
            logger.info(f"Interruption detected for session {session_id}")
            if state.current_agent_turn:
                state.current_agent_turn.status = "interrupted"
            if session_id in self.manager.audio_tasks:
                self.manager.audio_tasks[session_id].cancel()
                logger.info(f"Cancelled audio task for session {session_id}")
            await websocket.send_text(json.dumps({"type": "audio_interrupted"}))

    async def _handle_metrics_event(self, state: SessionState, event: dict, current_time: float, websocket: WebSocket):
        event_type = event.get("type")
        metrics = state.metrics
        metrics_to_send = {}
        if event_type == "input_audio_buffer.committed":
            if state.current_user_turn and state.current_user_turn.speech_start_time:
                stt_latency = (current_time - state.current_user_turn.speech_start_time) * 1000
                metrics_to_send["stt"] = round(stt_latency, 2)
        elif event_type in ("response.text.delta", "response.audio.delta"):
            if "llm_first_token_time" not in metrics and state.current_agent_turn and state.current_agent_turn.think_start_time:
                metrics["llm_first_token_time"] = current_time
                llm_latency = (current_time - state.current_agent_turn.think_start_time) * 1000
                metrics_to_send["llm"] = round(llm_latency, 2)
        elif event_type == "response.done":
            if state.total_cost > 0:
                metrics_to_send["cost"] = round(state.total_cost, 4)
            if "input_tokens" in metrics:
                metrics_to_send["input_tokens"] = metrics["input_tokens"]
            if "output_tokens" in metrics:
                metrics_to_send["output_tokens"] = metrics["output_tokens"]
        if metrics_to_send:
            await websocket.send_text(json.dumps({"type": "metrics", "data": metrics_to_send}))
