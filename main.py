import asyncio
from asyncio.log import logger
import json
import struct

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
            data = await websocket.receive_text()
            message = json.loads(data)

            if message["type"] == "audio":
                int16_data = message["data"]
                audio_bytes = struct.pack(f"{len(int16_data)}h", *int16_data)
                await manager.send_audio(session_id, audio_bytes)
            elif message["type"] == "text":
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
                    await websocket.send_text(
                        json.dumps({"type": "error", "error": "Empty text message."})
                    )
            elif message["type"] == "commit_audio":
                await manager.send_client_event(session_id, {"type": "input_audio_buffer.commit"})
            elif message["type"] == "client_vad_speech_start":
                asyncio.create_task(manager.interrupt(session_id))
                if session_id in manager.audio_tasks:
                    manager.audio_tasks[session_id].cancel()
                    logger.info(
                        "Cancelled audio task for session %s due to Client VAD",
                        session_id,
                    )
            elif message["type"] == "interrupt":
                asyncio.create_task(manager.interrupt(session_id))

    except WebSocketDisconnect:
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
