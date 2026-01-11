# Task: prevent-vad-interruption

1. Capture why playback is interrupted: the client-side VAD emits `client_vad_speech_start` + `handleRealtimeEvent({ type: 'client.speech_started' })` even when the agent is already speaking, so the UI/worker resets the queue and the server interrupts TTS.
2. In `static/app.js`, gate the VAD `onSpeechStart` callback behind the `isPlayingAudio` (or equivalent) flag so that any voice detected during playback is ignored, preventing both the local `stopAudioPlayback` call and the outgoing `client_vad_speech_start` message.
3. After the guard is in place, verify the flag resets after playback (`playback.drained` / `stopAudioPlayback`) so normal user speech still triggers VAD and the server still receives real speech-start events.
