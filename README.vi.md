# Voice Agent Open Source Toolkit (Tiếng Việt)

Bộ công cụ voice agent realtime gồm FastAPI backend, agent graph, static demo, và TTS từ Cartesia để thử nghiệm mã nguồn mở.

## Mục tiêu
- Dễ hiểu và dễ tùy biến cho team.
- Dễ scale từ 100 lên 10k rooms với 1 connection/room.
- Tối ưu event-loop bằng cơ chế backpressure + single-writer WebSocket.

## Kiến trúc tổng quan

```
Browser (VAD + recorder) ─┐
                          ├─> FastAPI WebSocket (/ws/{session_id})
Text input ---------------┘
                                  │
                                  │ (binary audio / JSON control)
                                  v
                        RealtimeWebSocketManager
                         - Connection per session_id
                         - incoming audio queue (backpressure)
                         - outgoing writer queue (single WS writer)
                                  │
                                  ├─> Azure OpenAI Realtime session
                                  │     (audio + text events)
                                  │
                                  └─> Cartesia TTS stream
                                        (audio chunks -> writer queue)
```

- **Single-writer WS**: mọi outbound send đi qua một writer task để tránh concurrent send.
- **Backpressure**: queue có `maxsize`, drop/throttle event spam khi client chậm.
- **Lazy session**: chỉ tạo model session khi có audio/text, giảm RAM khi nhiều room idle.

## Thành phần chính

- `main.py`: FastAPI app + WebSocket endpoint `/ws/{session_id}`.
- `agent/ws/manager.py`: quản lý session, backpressure, single-writer, TTS streaming (được re-export bởi `agent/companion.py`).
- `agent/core/dispatcher.py`: xử lý event, cập nhật state/metrics.
- `agent/core/tts_service.py`: chỉ stream audio chunks (không gửi WS trực tiếp).
- `static/`: giao diện web demo, VAD, audio worklets.

## Cách dùng (backend)

- **Audio**: ưu tiên gửi binary frames. JSON int16 vẫn hỗ trợ nhưng tốn CPU/RAM hơn.
- **Text**: gửi `{ "type": "text", "text": "..." }`.
- **Commit audio**: `{ "type": "commit_audio" }`.
- **Interrupt**: `{ "type": "interrupt" }` hoặc `client_vad_speech_start`.

## Checklist tối ưu

- Tuning queue: `WS_OUTGOING_MAX`, `WS_INCOMING_AUDIO_MAX`, `WS_TTS_CHUNK_BYTES`.
- Drop policy: `response.*.delta`/`metrics` có thể drop khi queue full.
- Audio binary để giảm JSON overhead.
- Thêm telemetry: queue depth, event-loop lag, drop count.

## Hướng phát triển

- Scale ngang: chạy nhiều instance + sticky routing theo `session_id`.
- Redis (tuỳ chọn): presence registry, rate limit, kick session cross-instance.
- Kafka (tuỳ chọn): stream analytics/audit, out-of-band.
- Celery (tuỳ chọn): job nền (billing, lưu transcript, summary).
