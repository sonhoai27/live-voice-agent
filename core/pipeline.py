import asyncio
import json
import time as _time
import logging
from typing import Any

logger = logging.getLogger(__name__)


class PipelineNode:
    """Base class for pipeline nodes with timing and error handling."""
    def __init__(self, name: str):
        self.name = name

    async def run(self, *args, **kwargs):
        start = _time.time()
        try:
            result = await self.process(*args, **kwargs)
            elapsed = (_time.time() - start) * 1000
            logger.info(f"[PipelineNode] {self.name} completed in {elapsed:.2f} ms")
            return result
        except Exception as e:
            logger.error(f"[PipelineNode] {self.name} error: {e}")
            # Emit pipeline error event if dispatcher is available
            dispatcher = kwargs.get("dispatcher")
            session_id = kwargs.get("session_id")
            if dispatcher and session_id:
                try:
                    event_type = f"{self.name.lower()}_failed"
                    error_event = {
                        "type": event_type,
                        "error": str(e),
                        "timestamp": _time.time(),
                    }
                    if hasattr(dispatcher, "standardize_event_payload"):
                        error_event = dispatcher.standardize_event_payload(error_event, participant_id=session_id, state="error")
                    if hasattr(dispatcher, "manager") and hasattr(dispatcher.manager, "websockets"):
                        ws = dispatcher.manager.websockets.get(session_id)
                        if ws:
                            asyncio.create_task(ws.send_text(json.dumps(error_event)))
                except Exception:
                    logger.exception("Failed to emit pipeline error event")
            raise

    async def process(self, *args, **kwargs):
        raise NotImplementedError


class STTNode(PipelineNode):
    def __init__(self):
        super().__init__("STTNode")

    async def healthcheck(self) -> bool:
        # Placeholder: always healthy
        return True

    async def process(self, audio_bytes):
        # TODO: Implement STT logic
        logger.info("[STTNode] Processing audio for transcription...")
        return "transcribed text"


class LLMNode(PipelineNode):
    def __init__(self):
        super().__init__("LLMNode")

    async def healthcheck(self) -> bool:
        # Placeholder: always healthy
        return True

    async def process(self, text):
        # TODO: Implement LLM inference logic
        logger.info("[LLMNode] Generating reply from LLM...")
        return "agent reply"
