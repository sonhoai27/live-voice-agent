import asyncio
import json
import logging
import time as _time
from typing import Optional, AsyncGenerator
from fastapi import WebSocket

from cartesia_tts import CartesiaTTS

logger = logging.getLogger(__name__)


class TTSService:
    """Decoupled TTS service: handles audio generation and streaming."""

    def __init__(self, cartesia_tts: Optional[CartesiaTTS] = None):
        self.cartesia_tts = cartesia_tts
        
    async def stream_audio(self, transcript: str) -> AsyncGenerator[bytes, None]:
        start = _time.time()
        try:
            async for chunk in self.cartesia_tts.get_audio_stream(transcript):
                yield chunk
            elapsed = (_time.time() - start) * 1000
            logger.info(f"[TTSService] stream_audio completed in {elapsed:.2f} ms")
        except Exception as e:
            logger.error(f"[TTSService] Error streaming audio: {e}")
            raise

    async def stream_to_websocket(self, session_id: str, transcript: str, websocket: WebSocket, on_first_chunk_callback: Optional[callable] = None) -> None:
        start = _time.time()
        try:
            await websocket.send_text(json.dumps({"type": "audio_start"}))
            buffer = bytearray()
            BUFFER_THRESHOLD = 4096
            first_chunk_sent = False
            total_streamed_bytes = 0

            async for audio_chunk in self.stream_audio(transcript):
                if not audio_chunk:
                    continue
                buffer.extend(audio_chunk)
                total_streamed_bytes += len(audio_chunk)

                while len(buffer) >= BUFFER_THRESHOLD:
                    chunk_to_send = bytes(buffer[:BUFFER_THRESHOLD])
                    await websocket.send_bytes(chunk_to_send)
                    del buffer[:BUFFER_THRESHOLD]
                    if not first_chunk_sent:
                        first_chunk_sent = True
                        if on_first_chunk_callback:
                            await on_first_chunk_callback()

            if len(buffer) > 0:
                await websocket.send_bytes(buffer)
                if not first_chunk_sent and on_first_chunk_callback:
                    await on_first_chunk_callback()
            elapsed = (_time.time() - start) * 1000
            logger.info(f"[TTSService] stream_to_websocket completed in {elapsed:.2f} ms for session {session_id}")
        except asyncio.CancelledError:
            logger.info(f"TTS streaming cancelled for session {session_id}")
            raise
        except Exception as e:
            logger.error(f"[TTSService] Error in stream_to_websocket for session {session_id}: {e}")
            raise
