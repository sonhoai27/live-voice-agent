# WebSocket scale plan (100 -> 10k rooms)

## Goals
- Không nghẽn event-loop: tách read/write/pump thành task riêng, không gửi WS từ nhiều nơi.
- Có backpressure: outbound/inbound queue có `maxsize`, có policy drop/throttle/close.
- Tối giản RAM: 1 object/room, state tối thiểu, lazy-init model session.
- Scale ngang: sticky routing theo `session_id`; Redis/Kafka chỉ dùng cho nhu cầu cross-instance (optional).

## Current bottlenecks
- Audio JSON int16 + `struct.pack(*list)` gây CPU/RAM spike.
- Outbound WS send diễn ra từ nhiều task (dispatcher + TTS) => dễ race/concurrent send và kẹt khi client chậm.
- Không giữ handle các background task => disconnect không cancel sạch.
- Connect tạo realtime session ngay lập tức => không scale nếu 10k rooms idle.

## Target design
### 1) Per-room `Connection`
- `session_id -> Connection(websocket, state, queues, tasks, realtime session)`
- Không dùng nhiều dict song song cho cùng 1 session.

### 2) Outbound backpressure (server -> client)
- `outgoing_queue: asyncio.Queue` + 1 writer task duy nhất.
- Mọi nơi (dispatcher/metrics/TTS) chỉ enqueue.
- Khi queue full:
  - Drop: `response.*.delta`, `metrics`, các event spam.
  - Block: `response.done`, `error`, `audio_start` (tuỳ).
  - (Option) close WS code `1013` nếu client quá chậm.

### 3) Inbound backpressure (client -> server/model)
- `incoming_audio_queue: asyncio.Queue` + audio-pump task.
- Receive loop chỉ parse tối thiểu + enqueue (không gọi trực tiếp `session.send_audio`).
- Audio protocol:
  - Ưu tiên binary frames (`receive_bytes`) nếu client support.
  - Fallback JSON int16 -> `array('h').tobytes()` (tránh `struct.pack(*list)`).

### 4) Lazy realtime session init
- `connect()` chỉ accept WS + start writer/pumps.
- `ensure_session()` chỉ chạy khi nhận text/audio/commit/interrupt.

### 5) Scale ngang
- LB sticky theo `session_id` (consistent-hash) để session không phải “move”.
- Redis (optional): presence/registry, kick session cross-instance, rate-limit.
- Kafka (optional): audit/analytics stream (out-of-band).
- Celery (optional): job nền (store transcript, billing, summarize), không nằm trên đường WS realtime.

## Tunables (env)
- `WS_OUTGOING_MAX`: queue maxsize (default 512)
- `WS_INCOMING_AUDIO_MAX`: queue maxsize (default 32)
- `WS_TTS_CHUNK_BYTES`: chunk size (default 4096)

