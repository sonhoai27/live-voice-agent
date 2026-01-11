# Voice Agent Open Source Toolkit

## Concept
An open-source realtime voice assistant that unites a FastAPI WebSocket backend (`main.py`), configurable agent workflows (`agent.py`), and a single-page static UI (`/static`). The stack streams microphone audio to Azure OpenAI’s realtime models, calls Cartesia for TTS, and visualizes the complete lifecycle (conversation, events, metrics) in the browser.

## Open Source intent
We designed this repo for community collaboration: the core agent routing is transparent, the client/server contracts are simple JSON streams, and all frontend assets are static so you can remix the UI. Feel free to submit bug reports, suggest new handoff agents, or expand the tooling/views. Adding tests for backend telemetry, writing feature requests, and documenting integration stories (Azure, Cartesia) are especially helpful.

## Overview
This repository wires together an OpenAI-powered realtime voice agent with a lightweight FastAPI backend (`main.py`), a configurable agent layer (`agent.py`), and a single-page frontend (`/static`). The stack lets you capture microphone audio, stream it to Azure OpenAI’s realtime endpoint, play back Cartesia-generated responses, surface tooling/handoff events, and monitor latency metrics inside the browser.

## Key components

- **`main.py`** – boots a FastAPI app that mounts `/static`, exposes a `/ws/{session_id}` WebSocket, and manages every realtime session via `RealtimeWebSocketManager`. The manager:
  - instantiates `RealtimeRunner` plus a typed `SessionState` for turn/metrics tracking,
  - funnels user speech/text through `send_audio`, `send_user_message`, or `send_client_event`,
  - serializes and dispatches events with `EventDispatcher`, and
  - kicks off `TTSService`/`CartesiaTTS` streaming (plus helper metrics) whenever `response.output_text.done` arrives.

- **`agent.py`** – returns the starting triage agent used by the runner. It registers three `RealtimeAgent` instances (`triage_agent`, `faq_agent`, `seat_booking_agent`), wires up simple tools (`faq_lookup_tool`, `update_seat`, `get_weather`), and chains handoffs so the triage agent can delegate or recall specialists depending on the customer goal. Customize this file to adjust instructions, add tools, or swap in a different agent graph.

- **`static/`** – a Tailwind-styled interface plus ancillary scripts that run entirely in the browser:
  - `index.html` renders the conversation pane, event stream, tools log, and controls (connect, mute, send text).
  - `app.js` defines `RealtimeDemo`, which opens the WebSocket, handles JSON events, renders transcripts/images, streams audio over `audio-recorder.worklet.js`, plays back assistant speech via `audio-playback.worklet.js`, tracks latency/cost metrics, and coordinates the client-side VAD (`vad.js`).
  - `vad.js` wraps `@ricky0123/vad-web` to trigger `client_vad_speech_start` events when the user begins speaking; it works together with the recorder worklet so recording only starts once the session is connected.

## Requirements

- Python 3.12+
- Install dependencies with `pip install -r requirements.txt` (or via `poetry install`/`pip install .` using `pyproject.toml`).
- Modern browser with Web Audio Worklet support (Chrome, Edge, Safari, etc.) for the frontend demo.

## Environment variables

Set these before running the backend (you can drop them in `.env` as shown):

- `AZURE_OPENAI_API_KEY` – Azure OpenAI API key with `realtime` access.
- `AZURE_OPENAI_REALTIME_URL` – The `wss://.../openai/v1/realtime` endpoint targeting your realtime model.
- `CARTESIA_API_KEY`, `CARTESIA_WEBSOCKET_URL`, `CARTESIA_API_VERSION`, `CARTESIA_MODEL_ID`, `CARTESIA_DEFAULT_LANGUAGE`, `CARTESIA_VOICE_ID` – Cartesia credentials + TTS configuration used by `CartesiaTTS`.

## Getting started

1. Create/refresh a Python virtual environment and install dependencies.
2. Copy `.env.example` (or update `.env`) with the keys above.
3. Launch the FastAPI server:

```bash
uvicorn main:app --host 0.0.0.0 --port 8001 --ws-max-size 16777216
```

4. Open `http://localhost:8001` (or hit the static UI via `/static/index.html`). Clicking **Connect** opens a WebSocket session (`ws://localhost:8001/ws/<session_id>`), turns on audio capture, and streams user speech to the realtime agent.

## Web UI behavior

- **Conversation pane** syncs every `message` event from the server, including transcripts, assistant responses, and media attachments. The UI deduplicates items by `item_id` and updates existing bubbles when history deltas arrive.
- **VAD + recorder**: `app.js` captures 24 kHz mono audio, forwards Int16 chunks as JSON arrays, and observes client-side VAD events to interrupt playback or rerun the agent.
- **Playback**: assistant TTS chunks are decoded from base64 or raw Int16, aggregated, applied with fade-in/out, and routed through `audio-playback.worklet.js`. Interruptions cancel playback and drop pending chunks.
- **Metrics panel** shows TTS/LLM/STT latencies, turn duration, token counts, and cost each time the backend emits a `metrics` event.
- **Tools & events panels** display handoff/tool lifecycle events and the raw event stream for debugging.

## Customization tips

- Swap the starting agent by returning a different `RealtimeAgent` from `get_starting_agent`.
- Extend `agent.py` by adding more `function_tool` helpers and append them to agent graphs; the front end automatically surfaces `tool_start`/`tool_end` events.
- The backend uses `SessionState.metrics` to store STT/TTS timing—update `_send_metrics` (lines 200-250) if you need additional telemetry.
- You can mount other static assets or rewrite `index.html` to match your branding; `main.py` already serves `/static` and `index.html` at `/`.

## Troubleshooting

- If the browser cannot connect, double-check the WebSocket URL/port and make sure `uvicorn` is running.
- Audio capture fails if `navigator.mediaDevices` is unsupported; use a secure context (HTTPS or `localhost`).
- Cartesia/RealTime failures show up in the backend logs; look for `self.tts_service.stream_to_websocket` or `RealtimeRunner` errors in `uvicorn` output.
