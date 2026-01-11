import asyncio
from asyncio.log import logger
import json
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent.companion import manager
from agents.realtime.config import RealtimeUserInputMessage

async def lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(websocket, session_id)
    try:
        while True:
            packet: dict[str, Any] = await websocket.receive()
            if packet.get("type") == "websocket.disconnect":
                break
            if packet.get("bytes") is not None:
                await manager.send_audio(session_id, packet["bytes"])
                continue
            if packet.get("text") is None:
                continue

            message = json.loads(packet["text"])
            msg_type = message.get("type")

            if msg_type == "audio":
                int16_data = message.get("data") or []
                audio_bytes = manager.parse_json_int16_audio(int16_data)
                await manager.send_audio(session_id, audio_bytes)
            elif msg_type == "text":
                text_content = message.get("text")
                logger.info("Received text message from client (session %s): %s", session_id, text_content)
                if text_content:
                    user_msg: RealtimeUserInputMessage = {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text_content}],
                    }
                    await manager.send_user_message(session_id, user_msg)
                else:
                    await manager.send_json(session_id, {"type": "error", "error": "Empty text message."}, drop_if_full=True)
            elif msg_type == "commit_audio":
                await manager.send_client_event(session_id, {"type": "input_audio_buffer.commit"})
            elif msg_type == "client_vad_speech_start":
                asyncio.create_task(manager.interrupt(session_id))
                await manager.cancel_tts(session_id)
            elif msg_type == "interrupt":
                asyncio.create_task(manager.interrupt(session_id))

    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(session_id)


app.mount("/", StaticFiles(directory="static", html=True), name="static")


@app.get("/")
async def read_index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        ws_max_size=16 * 1024 * 1024,
    )
