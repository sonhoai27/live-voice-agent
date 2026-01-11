import asyncio
import base64
import json
import logging
import os
import struct
import array
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing_extensions import assert_never

# Load environment variables from .env file
load_dotenv()


def convert_float32_to_int16(audio_bytes: bytes) -> bytes:
    """
    Convert Cartesia float32 audio to int16 format for audio worklet.
    
    Args:
        audio_bytes: Raw PCM float32 audio data from Cartesia (little-endian)
        
    Returns:
        PCM int16 audio data (little-endian)
    """
    # Calculate number of samples (4 bytes per float32)
    num_samples = len(audio_bytes) // 4
    
    if num_samples == 0:
        return b''
    
    # Unpack float32 samples (little-endian)
    float_samples = struct.unpack(f'<{num_samples}f', audio_bytes)
    
    # Convert to int16
    int16_samples = []
    for sample in float_samples:
        # Clamp between -1.0 and 1.0
        sample = max(-1.0, min(1.0, sample))
        # Scale to int16 range (-32768 to 32767)
        int16_sample = int(sample * 32767)
        int16_samples.append(int16_sample)
    
    # Pack int16 samples (little-endian)
    return struct.pack(f'<{num_samples}h', *int16_samples)

from agents.realtime import RealtimeRunner, RealtimeSession, RealtimeSessionEvent
from agents.realtime.config import RealtimeUserInputMessage, RealtimeRunConfig
from agents.realtime.items import RealtimeItem
from agents.realtime.model import RealtimeModelConfig
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

# Import Cartesia TTS service
from agent.cartesia_tts import CartesiaTTS, get_cartesia_tts

# Import TwilioHandler class - handle both module and package use cases
if TYPE_CHECKING:
    # For type checking, use the relative import
    from .agent import get_starting_agent
else:
    # At runtime, try both import styles
    try:
        # Try relative import first (when used as a package)
        from .agent import get_starting_agent
    except ImportError:
        # Fall back to direct import (when run as a script)
        from agent import get_starting_agent


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RealtimeWebSocketManager:
    def __init__(self):
        self.active_sessions: dict[str, RealtimeSession] = {}
        self.session_contexts: dict[str, Any] = {}
        self.websockets: dict[str, WebSocket] = {}
        self.cartesia_tts: Optional[CartesiaTTS] = None
        self.use_cartesia_tts: bool = False  # Flag to enable/disable Cartesia TTS

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
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                    "interrupt_response": True,
                    "create_response": True,
                },
                "input_audio_transcription": {
                    "model": "gpt-4o-transcribe"
                },
                "output_modalities": ['text']  # Only text output when using Cartesia TTS
            },
            "api_key": os.getenv("AZURE_OPENAI_API_KEY"),
            "url": os.getenv("AZURE_OPENAI_REALTIME_URL"),
        }
        session_context = await runner.run(model_config=model_config)
        session = await session_context.__aenter__()
        self.active_sessions[session_id] = session
        self.session_contexts[session_id] = session_context

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
    
    def enable_cartesia_tts(
        self,
        model_id: str = "sonic-3",
        voice_id: str = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b",
        sample_rate: int = 44100
    ) -> None:
        """
        Enable Cartesia TTS for audio generation.
        
        Args:
            model_id: Cartesia model ID (default: sonic-3)
            voice_id: Voice ID to use
            sample_rate: Audio sample rate (default: 44100)
        """
        try:
            self.cartesia_tts = CartesiaTTS(
                model_id=model_id,
                voice_id=voice_id,
                sample_rate=sample_rate
            )
            self.use_cartesia_tts = True
            logger.info(f"Cartesia TTS enabled with model: {model_id}, voice: {voice_id}")
        except Exception as e:
            logger.error(f"Failed to enable Cartesia TTS: {e}")
            raise
    
    def disable_cartesia_tts(self) -> None:
        """Disable Cartesia TTS and revert to OpenAI TTS."""
        self.use_cartesia_tts = False
        self.cartesia_tts = None
        logger.info("Cartesia TTS disabled, using OpenAI TTS")
    
    async def generate_cartesia_audio(self, transcript: str) -> Optional[bytes]:
        """
        Generate audio using Cartesia TTS from a text transcript.
        
        Args:
            transcript: Text to convert to speech
            
        Returns:
            Audio bytes if successful, None otherwise
        """
        if not self.use_cartesia_tts or not self.cartesia_tts:
            logger.warning("Cartesia TTS is not enabled")
            return None
        
        try:
            audio_bytes = await self.cartesia_tts.get_audio_async(transcript)
            logger.debug(f"Generated {len(audio_bytes)} bytes of audio with Cartesia")
            return audio_bytes
        except Exception as e:
            logger.error(f"Error generating audio with Cartesia: {e}")
            return None

    async def _process_events(self, session_id: str):
        try:
            session = self.active_sessions[session_id]
            websocket = self.websockets[session_id]

            async for event in session:
                event_data = await self._serialize_event(event)
                
                # If Cartesia TTS is enabled and we have a text response, generate audio

                event_type = (
                    event_data.get("type")
                    if isinstance(event_data, dict)
                    else getattr(event_data, "type", None)
                )
                
                transcript = (
                    event_data.get("transcript")
                    if isinstance(event_data, dict)
                    else getattr(event_data, "transcript", None)
                )
                if self.use_cartesia_tts and event_type == "response.output_text.done" and transcript != None:
                    # Extract transcript from the event
                    if transcript:
                        logger.info(f"Generating audio for transcript: {transcript[:50]}...")
                        # Stream audio with Cartesia
                        async for audio_chunk in self.stream_cartesia_audio(transcript):
                            if audio_chunk:
                                # Send audio event to client
                                audio_event = {
                                    "type": "audio",
                                    "audio": base64.b64encode(audio_chunk).decode("utf-8"),
                                    "source": "cartesia"
                                }
                                await websocket.send_text(json.dumps(audio_event))
                
                await websocket.send_text(json.dumps(event_data))
        except Exception as e:
            print(e)
            logger.error(f"Error processing events for session {session_id}: {e}")
    
    async def stream_cartesia_audio(self, transcript: str) -> AsyncGenerator[bytes, None]:
        """
        Stream audio using Cartesia TTS from a text transcript.
        
        Args:
            transcript: Text to convert to speech
            
        Yields:
             Audio bytes chunks
        """
        if not self.use_cartesia_tts or not self.cartesia_tts:
             logger.warning("Cartesia TTS is not enabled")
             return
        
        try:
             async for chunk in self.cartesia_tts.get_audio_stream(transcript):
                 yield chunk
        except Exception as e:
             logger.error(f"Error streaming audio with Cartesia: {e}")
             return
    
    def _extract_transcript_from_event(self, event: RealtimeSessionEvent) -> Optional[str]:
        """
        Extract transcript text from a raw_model_event.
        
        Args:
            event: The realtime session event
            
        Returns:
            Transcript text if found, None otherwise
        """
        if event.type != "raw_model_event":
            return None
        
        try:
            raw = event.data
            data = getattr(raw, "data", None) or getattr(raw, "message", None) or raw
            
            if isinstance(data, dict):
                # Check for transcript at top level
                if "transcript" in data:
                    return data["transcript"]
                
                # Check for transcript in item/content
                item = data.get("item") or data.get("conversation_item")
                if isinstance(item, dict):
                    content = item.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and "transcript" in part:
                                return part["transcript"]
            
            return None
        except Exception as e:
            logger.error(f"Error extracting transcript from event: {e}")
            return None

    def _sanitize_history_item(self, item: RealtimeItem) -> dict[str, Any]:
        """Remove large binary payloads from history items while keeping transcripts."""
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
    
    def unwrap_data(self, x):
        """
        Trả về dữ liệu bên trong theo kiểu an toàn:
        - nếu x là dict: trả về x
        - nếu x là object có .data: bóc tiếp
        - nếu không bóc được: trả về None
        """
        while True:
            if x is None:
                return None
            if isinstance(x, dict):
                return x
            if hasattr(x, "data"):
                x = getattr(x, "data")
                continue
            return None

    async def _serialize_event(self, event: RealtimeSessionEvent) -> dict[str, Any]:
        base_event: dict[str, Any] = {
            "type": event.type,
        }
        
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
            base_event["audio"] = base64.b64encode(event.audio.data).decode("utf-8")
        elif event.type == "audio_interrupted":
            pass
        elif event.type == "audio_end":
            pass
        elif event.type == "history_updated":
            base_event["history"] = [self._sanitize_history_item(item) for item in event.history]
        elif event.type == "history_added":
            # Provide the added item so the UI can render incrementally.
            try:
                base_event["item"] = self._sanitize_history_item(event.item)
            except Exception:
                base_event["item"] = None
        elif event.type == "guardrail_tripped":
            base_event["guardrail_results"] = [
                {"name": result.guardrail.name} for result in event.guardrail_results
            ]
        elif event.type == "raw_model_event":
            # event.data thường là object có .type và payload (dict-like)
            raw = event.data
            base_event["raw_model_event"] = {"type": raw.type}
            
            if getattr(raw, "type", None) == "raw_server_event":
                payload = self.unwrap_data(getattr(raw, "data", None))  # payload cuối cùng dạng dict
 
                if payload and payload.get("type") == "response.output_text.done":
                    base_event["type"] = "response.output_text.done"
                    base_event["transcript"] = payload.get("text", "")
                pass
            # cố gắng trích transcript theo vài pattern phổ biến
            data = getattr(raw, "data", None) or getattr(raw, "message", None) or raw
            # nếu data là dict-like:
            if isinstance(data, dict):
                # 1) transcript ở top-level
                if "transcript" in data:
                    base_event["raw_model_event"]["transcript"] = data["transcript"]

                # 2) transcript nằm trong item/content
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
        elif event.type == "input_audio_timeout_triggered":
            pass
        else:
            assert_never(event)

        return base_event


manager = RealtimeWebSocketManager()

# Initialize Cartesia TTS if API key is available
if os.getenv("CARTESIA_API_KEY"):
    try:
        model_id = os.getenv("CARTESIA_MODEL_ID", "sonic-3")
        voice_id = os.getenv("CARTESIA_VOICE_ID", "6ccbfb76-1fc6-48f7-b71d-91ac6298247b")
        sample_rate = int(os.getenv("CARTESIA_SAMPLE_RATE", "44100"))
        
        manager.enable_cartesia_tts(
            model_id=model_id,
            voice_id=voice_id,
            sample_rate=sample_rate
        )
        logger.info("Cartesia TTS initialized successfully")
    except Exception as e:
        logger.warning(f"Failed to initialize Cartesia TTS: {e}")
        logger.info("Falling back to OpenAI TTS")
else:
    logger.info("CARTESIA_API_KEY not set, using OpenAI TTS")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(websocket, session_id)
    image_buffers: dict[str, dict[str, Any]] = {}
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message["type"] == "audio":
                # Convert int16 array to bytes
                int16_data = message["data"]
                audio_bytes = struct.pack(f"{len(int16_data)}h", *int16_data)
                await manager.send_audio(session_id, audio_bytes)
            elif message["type"] == "image":
                logger.info("Received image message from client (session %s).", session_id)
                # Build a conversation.item.create with input_image (and optional input_text)
                data_url = message.get("data_url")
                prompt_text = message.get("text") or "Please describe this image."
                if data_url:
                    logger.info(
                        "Forwarding image (structured message) to Realtime API (len=%d).",
                        len(data_url),
                    )
                    user_msg: RealtimeUserInputMessage = {
                        "type": "message",
                        "role": "user",
                        "content": (
                            [
                                {"type": "input_image", "image_url": data_url, "detail": "high"},
                                {"type": "input_text", "text": prompt_text},
                            ]
                            if prompt_text
                            else [{"type": "input_image", "image_url": data_url, "detail": "high"}]
                        ),
                    }
                    await manager.send_user_message(session_id, user_msg)
                    # Acknowledge to client UI
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "client_info",
                                "info": "image_enqueued",
                                "size": len(data_url),
                            }
                        )
                    )
                else:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "error": "No data_url for image message.",
                            }
                        )
                    )
            elif message["type"] == "commit_audio":
                # Force close the current input audio turn
                await manager.send_client_event(session_id, {"type": "input_audio_buffer.commit"})
            elif message["type"] == "image_start":
                img_id = str(message.get("id"))
                image_buffers[img_id] = {
                    "text": message.get("text") or "Please describe this image.",
                    "chunks": [],
                }
                await websocket.send_text(
                    json.dumps({"type": "client_info", "info": "image_start_ack", "id": img_id})
                )
            elif message["type"] == "image_chunk":
                img_id = str(message.get("id"))
                chunk = message.get("chunk", "")
                if img_id in image_buffers:
                    image_buffers[img_id]["chunks"].append(chunk)
                    if len(image_buffers[img_id]["chunks"]) % 10 == 0:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "client_info",
                                    "info": "image_chunk_ack",
                                    "id": img_id,
                                    "count": len(image_buffers[img_id]["chunks"]),
                                }
                            )
                        )
            elif message["type"] == "image_end":
                img_id = str(message.get("id"))
                buf = image_buffers.pop(img_id, None)
                if buf is None:
                    await websocket.send_text(
                        json.dumps({"type": "error", "error": "Unknown image id for image_end."})
                    )
                else:
                    data_url = "".join(buf["chunks"]) if buf["chunks"] else None
                    prompt_text = buf["text"]
                    if data_url:
                        logger.info(
                            "Forwarding chunked image (structured message) to Realtime API (len=%d).",
                            len(data_url),
                        )
                        user_msg2: RealtimeUserInputMessage = {
                            "type": "message",
                            "role": "user",
                            "content": (
                                [
                                    {
                                        "type": "input_image",
                                        "image_url": data_url,
                                        "detail": "high",
                                    },
                                    {"type": "input_text", "text": prompt_text},
                                ]
                                if prompt_text
                                else [
                                    {"type": "input_image", "image_url": data_url, "detail": "high"}
                                ]
                            ),
                        }
                        await manager.send_user_message(session_id, user_msg2)
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "client_info",
                                    "info": "image_enqueued",
                                    "id": img_id,
                                    "size": len(data_url),
                                }
                            )
                        )
                    else:
                        await websocket.send_text(
                            json.dumps({"type": "error", "error": "Empty image."})
                        )
            elif message["type"] == "interrupt":
                await manager.interrupt(session_id)

    except WebSocketDisconnect:
        await manager.disconnect(session_id)


app.mount("/", StaticFiles(directory="static", html=True), name="static")


@app.get("/")
async def read_index():
    return FileResponse("static/index.html")


@app.post("/api/tts/enable")
async def enable_cartesia_tts(
    model_id: str = "sonic-3",
    voice_id: str = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b",
    sample_rate: int = 44100
):
    """
    Enable Cartesia TTS for audio generation.
    
    Args:
        model_id: Cartesia model ID (default: sonic-3)
        voice_id: Voice ID to use
        sample_rate: Audio sample rate (default: 44100)
    
    Returns:
        Success message
    """
    try:
        manager.enable_cartesia_tts(
            model_id=model_id,
            voice_id=voice_id,
            sample_rate=sample_rate
        )
        return {"status": "success", "message": "Cartesia TTS enabled"}
    except Exception as e:
        logger.error(f"Error enabling Cartesia TTS: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/tts/disable")
async def disable_cartesia_tts():
    """
    Disable Cartesia TTS and revert to OpenAI TTS.
    
    Returns:
        Success message
    """
    manager.disable_cartesia_tts()
    return {"status": "success", "message": "Cartesia TTS disabled"}


@app.get("/api/tts/status")
async def get_tts_status():
    """
    Get the current TTS status.
    
    Returns:
        Status information about which TTS service is active
    """
    return {
        "status": "success",
        "use_cartesia_tts": manager.use_cartesia_tts,
        "cartesia_enabled": manager.cartesia_tts is not None
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        # Increased WebSocket frame size to comfortably handle image data URLs.
        ws_max_size=16 * 1024 * 1024,
    )
