"""
Cartesia TTS Service Module

This module provides integration with Cartesia's Text-to-Speech API
for generating audio from text responses.
"""

import os
import asyncio
import logging
from typing import Optional, AsyncGenerator, Any
from cartesia import Cartesia

logger = logging.getLogger(__name__)


class CartesiaTTS:
    """
    Wrapper for Cartesia Text-to-Speech API with async support.
    
    This service converts text transcripts to audio using Cartesia's
    high-quality TTS models.
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model_id: str = "sonic-3",
        voice_id: str = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b",
        sample_rate: int = 24000,
        encoding: str = "pcm_s16le",  # Use pcm_s16le for direct binary streaming
        container: str = "raw"  # Raw PCM data without container
    ):
        """
        Initialize Cartesia TTS service.
        
        Args:
            api_key: Cartesia API key (defaults to CARTESIA_API_KEY env var)
            model_id: Cartesia model ID to use (default: sonic-3)
            voice_id: Voice ID to use for generation
            sample_rate: Audio sample rate (default: 44100)
            encoding: Audio encoding format (default: pcm_f32le)
            container: Audio container format (default: wav)
        """
        self.api_key = api_key or os.getenv("CARTESIA_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Cartesia API key is required. Set CARTESIA_API_KEY environment variable "
                "or pass api_key parameter."
            )
        
        self.model_id = model_id
        self.voice_id = voice_id
        self.sample_rate = sample_rate
        self.encoding = encoding
        self.container = container
        
        # Initialize Cartesia client
        self.client = Cartesia(api_key=self.api_key)
        logger.info(f"Cartesia TTS initialized with model: {model_id}, voice: {voice_id}")
    
    def get_audio(self, transcript: str) -> bytes:
        """
        Convert text transcript to audio bytes.
        
        Args:
            transcript: Text to convert to speech
            
        Returns:
            Audio data as bytes in the specified format
            
        Raises:
            Exception: If TTS generation fails
        """
        try:
            logger.debug(f"Generating audio for transcript: {transcript[:50]}...")
            
            chunk_iter = self.client.tts.bytes(
                model_id=self.model_id,
                transcript=transcript,
                voice={
                    "mode": "id",
                    "id": self.voice_id,
                },
                output_format={
                    "container": self.container,
                    "sample_rate": self.sample_rate,
                    "encoding": self.encoding,
                },
            )
            
            # Collect all chunks into a single bytes object
            audio_buffer = bytearray()
            for chunk in chunk_iter:
                audio_buffer.extend(chunk)
            audio_data = bytes(audio_buffer)
            
            logger.debug(f"Generated {len(audio_data)} bytes of audio")
            return audio_data
            
        except Exception as e:
            logger.error(f"Error generating audio with Cartesia: {e}")
            raise
    
    async def get_audio_async(self, transcript: str) -> bytes:
        """
        Async version of get_audio.
        
        Args:
            transcript: Text to convert to speech
            
        Returns:
            Audio data as bytes in the specified format
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_audio, transcript)
    
    async def get_audio_stream(self, transcript: str) -> AsyncGenerator[bytes, None]:
        """
        Stream audio generation for real-time playback.
        
        Args:
            transcript: Text to convert to speech
            
        Yields:
            Audio chunks as they are generated
        """
        logger.debug(f"Streaming audio for transcript: {transcript[:50]}...")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def produce_chunks() -> None:
            """Run the blocking Cartesia iterator in a thread and push chunks back to the loop."""
            try:
                chunk_iter = self.client.tts.bytes(
                    model_id=self.model_id,
                    transcript=transcript,
                    voice={
                        "mode": "id",
                        "id": self.voice_id,
                    },
                    output_format={
                        "container": self.container,
                        "sample_rate": self.sample_rate,
                        "encoding": self.encoding,
                    },
                )
                for chunk in chunk_iter:
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        producer_task = asyncio.create_task(asyncio.to_thread(produce_chunks))
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "chunk":
                    yield payload
                elif kind == "error":
                    raise payload
                elif kind == "done":
                    break
        except asyncio.CancelledError:
            producer_task.cancel()
            raise
        except Exception as exc:
            logger.error(f"Error streaming audio with Cartesia: {exc}")
            raise
        finally:
            try:
                await producer_task
            except asyncio.CancelledError:
                pass
    
    def update_voice(self, voice_id: str) -> None:
        """
        Update the voice ID for TTS generation.
        
        Args:
            voice_id: New voice ID to use
        """
        self.voice_id = voice_id
        logger.info(f"Updated Cartesia voice ID to: {voice_id}")
    
    def update_model(self, model_id: str) -> None:
        """
        Update the model ID for TTS generation.
        
        Args:
            model_id: New model ID to use
        """
        self.model_id = model_id
        logger.info(f"Updated Cartesia model ID to: {model_id}")


# Singleton instance for easy access across the application
_cartesia_tts_instance: Optional[CartesiaTTS] = None


def get_cartesia_tts() -> CartesiaTTS:
    """
    Get or create the singleton CartesiaTTS instance.
    
    Returns:
        CartesiaTTS instance
    """
    global _cartesia_tts_instance
    if _cartesia_tts_instance is None:
        _cartesia_tts_instance = CartesiaTTS()
    return _cartesia_tts_instance


def reset_cartesia_tts() -> None:
    """Reset the singleton CartesiaTTS instance."""
    global _cartesia_tts_instance
    _cartesia_tts_instance = None
