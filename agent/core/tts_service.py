import logging
import time as _time
from typing import Optional, AsyncGenerator

from ..cartesia_tts import CartesiaTTS

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
