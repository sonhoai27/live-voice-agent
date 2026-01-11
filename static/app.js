class RealtimeDemo {
    constructor() {
        this.ws = null;
        this.isConnected = false;
        this.isMuted = false;
        this.isCapturing = false;
        this.audioContext = null;
        this.captureSource = null;
        this.captureNode = null;
        this.stream = null;
        this.sessionId = this.generateSessionId();

        this.isPlayingAudio = false;
        this.playbackAudioContext = null;
        this.playbackNode = null;
        this.playbackInitPromise = null;
        this.pendingPlaybackChunks = [];
        this.playbackFadeSec = 0.02; // ~20ms fade to reduce clicks
        this.messageNodes = new Map(); // item_id -> DOM node
        this.seenItemIds = new Set(); // item_id set for append-only syncing

        this.initializeElements();
        this.setupEventListeners();
    }

    initializeElements() {
        this.connectBtn = document.getElementById('connectBtn');
        this.muteBtn = document.getElementById('muteBtn');
        this.messageInput = document.getElementById('messageInput');
        this.sendBtn = document.getElementById('sendBtn');
        this.status = document.getElementById('status');
        this.messagesContent = document.getElementById('messagesContent');
        this.eventsContent = document.getElementById('eventsContent');
        this.toolsContent = document.getElementById('toolsContent');

        // Initialize VAD
        this.vadManager = new VADManager();
        this.vadManager.initialize(
            () => {
                if (this.isPlayingAudio) {
                    console.log('Client VAD: Speech Start ignored while playback is active');
                    return;
                }
                // On Speech Start
                console.log('Client VAD: Speech Started');
                this.handleRealtimeEvent({ type: 'client.speech_started' });
                // Optional: Send event to server if needed
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    console.log("!!! Sending client_vad_speech_start !!!");
                    this.ws.send(JSON.stringify({ type: 'client_vad_speech_start' }));
                } else {
                    console.warn("Cannot send VAD event - WebSocket not open");
                }
            },
            (audio) => {
                // On Speech End
                console.log('Client VAD: Speech Ended');
            }
        ).then(result => {
            console.log("VAD Manager initialized:", result);
        });

        // Metrics Panel
        this.metricsPanel = document.createElement('div');
        this.metricsPanel.id = 'metricsPanel';
        // Add styles for the flash effect
        const style = document.createElement('style');
        style.textContent = `
            .metric-value {
                font-weight: bold;
                transition: color 0.3s ease;
            }
            .metric-updated {
                color: #fff !important;
                text-shadow: 0 0 5px #0f0;
            }
            #metricsPanel {
                font-family: 'Courier New', Courier, monospace;
            }
        `;
        document.head.appendChild(style);

        this.metricsPanel.style.cssText = `
            position: fixed;
            top: 160px;
            right: 20px;
            background: rgba(0, 0, 0, 0.85);
            color: #00ff00;
            padding: 15px;
            border-radius: 8px;
            font-size: 14px;
            z-index: 9999;
            display: none;
            backdrop-filter: blur(5px);
            border: 1px solid #004400;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
            pointer-events: none;
        `;
        this.metricsPanel.innerHTML = `
            <div style="margin-bottom:4px; display:flex; justify-content:space-between; width:140px;"><span>TTS:</span> <span id="metric-tts" class="metric-value">-- ms</span></div>
            <div style="margin-bottom:4px; display:flex; justify-content:space-between; width:140px;"><span>LLM:</span> <span id="metric-llm" class="metric-value">-- ms</span></div>
            <div style="margin-bottom:4px; display:flex; justify-content:space-between; width:140px;"><span>STT:</span> <span id="metric-stt" class="metric-value">-- ms</span></div>
            <div style="margin-bottom:4px; display:flex; justify-content:space-between; width:140px;"><span>TURN:</span> <span id="metric-turn" class="metric-value">-- ms</span></div>
            <div style="margin-bottom:4px; display:flex; justify-content:space-between; width:140px; border-top:1px solid #333; padding-top:4px;"><span>IN:</span> <span id="metric-input_tokens" class="metric-value">--</span></div>
            <div style="margin-bottom:4px; display:flex; justify-content:space-between; width:140px;"><span>OUT:</span> <span id="metric-output_tokens" class="metric-value">--</span></div>
            <div style="margin-bottom:0px; display:flex; justify-content:space-between; width:140px;"><span>COST:</span> <span id="metric-cost" class="metric-value">$0.0000</span></div>
        `;
        document.body.appendChild(this.metricsPanel);
    }

    setupEventListeners() {
        this.connectBtn.addEventListener('click', () => {
            if (this.isConnected) {
                this.disconnect();
            } else {
                this.connect();
            }
        });

        this.muteBtn.addEventListener('click', () => {
            this.toggleMute();
        });

        // Text message sending
        this.sendBtn.addEventListener('click', () => {
            this.sendTextMessage();
        });

        this.messageInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                this.sendTextMessage();
            }
        });

        // Clear events button
        document.getElementById('clearEventsBtn').addEventListener('click', () => {
            this.eventsContent.innerHTML = '';
        });

    }

    sendTextMessage() {
        const text = this.messageInput.value.trim();
        if (!text) return;

        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.stopAudioPlayback();
            this.ws.send(JSON.stringify({ type: 'text', text: text }));
            this.addMessage('user', text);
            this.messageInput.value = '';
        } else {
            console.warn('Not connected; message will not be sent. Click Connect first.');
        }
    }

    generateSessionId() {
        return 'session_' + Math.random().toString(36).substr(2, 9);
    }

    async connect() {
        try {
            this.ws = new WebSocket(`wss://live-voice-agent-nine.vercel.app/ws/${this.sessionId}`);
            this.ws.binaryType = 'arraybuffer';

            this.ws.onopen = async () => {
                this.isConnected = true;
                this.updateConnectionUI();
                await this.startContinuousCapture();
                if (this.vadManager && this.stream) {
                    this.vadManager.start(this.stream);
                }
            };

            this.ws.onmessage = (event) => {
                if (event.data instanceof ArrayBuffer) {
                    if (this.ignoreIncomingAudio) {
                        return;
                    }
                    const int16Array = new Int16Array(event.data);
                    this.playAudioRaw(int16Array);
                    return;
                }
                const data = JSON.parse(event.data);
                this.handleRealtimeEvent(data);
            };

            this.ws.onclose = () => {
                this.isConnected = false;
                this.updateConnectionUI();
            };

            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };

        } catch (error) {
            console.error('Failed to connect:', error);
        }
    }

    updateStatus(text, className = '') {
        this.status.textContent = text;
        this.status.className = 'status ' + className;
    }

    disconnect() {
        if (this.ws) {
            this.ws.close();
        }
        this.stopContinuousCapture();
        if (this.vadManager) this.vadManager.stop();
    }

    updateConnectionUI() {
        if (this.isConnected) {
            this.connectBtn.textContent = 'Disconnect';
            this.connectBtn.className = 'connect-btn connected';
            this.status.textContent = 'Connected';
            this.status.className = 'status connected';
            this.muteBtn.disabled = false;
            this.metricsPanel.style.display = 'block';
        } else {
            this.connectBtn.textContent = 'Connect';
            this.connectBtn.className = 'connect-btn disconnected';
            this.status.textContent = 'Disconnected';
            this.status.className = 'status disconnected';
            this.muteBtn.disabled = true;
            this.metricsPanel.style.display = 'none';
        }
    }

    toggleMute() {
        this.isMuted = !this.isMuted;
        this.updateMuteUI();
    }

    updateMuteUI() {
        if (this.isMuted) {
            this.muteBtn.textContent = '🔇 Off';
            this.muteBtn.className = 'mute-btn muted';
        } else {
            this.muteBtn.textContent = '🎤 On';
            this.muteBtn.className = 'mute-btn unmuted';
            if (this.isCapturing) {
                this.muteBtn.classList.add('active');
            }
        }
    }



    async startContinuousCapture() {
        if (!this.isConnected || this.isCapturing) return;

        // Check if getUserMedia is available
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error('getUserMedia not available. Please use HTTPS or localhost.');
        }

        try {
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    sampleRate: 24000,
                    channelCount: 1,
                    echoCancellation: true,
                    noiseSuppression: true
                }
            });

            this.audioContext = new AudioContext({ sampleRate: 24000, latencyHint: 'interactive' });
            if (this.audioContext.state === 'suspended') {
                try { await this.audioContext.resume(); } catch { }
            }

            if (!this.audioContext.audioWorklet) {
                throw new Error('AudioWorklet API not supported in this browser.');
            }

            await this.audioContext.audioWorklet.addModule('audio-recorder.worklet.js');

            this.captureSource = this.audioContext.createMediaStreamSource(this.stream);
            this.captureNode = new AudioWorkletNode(this.audioContext, 'pcm-recorder');

            this.captureNode.port.onmessage = (event) => {
                if (this.isMuted) return;
                if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

                const chunk = event.data instanceof ArrayBuffer ? new Int16Array(event.data) : event.data;
                if (!chunk || !(chunk instanceof Int16Array) || chunk.length === 0) return;

                this.ws.send(JSON.stringify({
                    type: 'audio',
                    data: Array.from(chunk)
                }));
            };

            this.captureSource.connect(this.captureNode);
            this.captureNode.connect(this.audioContext.destination);

            this.isCapturing = true;
            this.updateMuteUI();

        } catch (error) {
            console.error('Failed to start audio capture:', error);
        }
    }

    stopContinuousCapture() {
        if (!this.isCapturing) return;

        this.isCapturing = false;

        if (this.captureSource) {
            try { this.captureSource.disconnect(); } catch { }
            this.captureSource = null;
        }

        if (this.captureNode) {
            this.captureNode.port.onmessage = null;
            try { this.captureNode.disconnect(); } catch { }
            this.captureNode = null;
        }

        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }

        if (this.stream) {
            this.stream.getTracks().forEach(track => track.stop());
            this.stream = null;
        }

        this.updateMuteUI();
    }

    handleRealtimeEvent(event) {
        // Add to raw events pane
        this.addRawEvent(event);

        // Add to tools panel if it's a tool or handoff event
        if (event.type === 'tool_start' || event.type === 'tool_end' || event.type === 'handoff') {
            this.addToolEvent(event);
        }

        // Handle specific event types
        switch (event.type) {
            case 'audio':
                this.playAudio(event.audio);
                break;
            case 'audio_interrupted':
            case 'input_audio_buffer.speech_started':
                console.log('Interruption event received:', event.type);
                this.stopAudioPlayback();
                break;
            case 'input_audio_timeout_triggered':
                // Ask server to commit the input buffer to expedite model response
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    this.ws.send(JSON.stringify({ type: 'commit_audio' }));
                }
                break;
            case 'history_updated':
                this.syncMissingFromHistory(event.history);
                this.updateLastMessageFromHistory(event.history);
                break;
            case 'history_added':
                // Append just the new item without clearing the thread.
                if (event.item) {
                    this.addMessageFromItem(event.item);
                }
                break;
            case 'raw_model_event':
                // Check if this is a transcript event as requested by user
                if (event.raw_model_event &&
                    event.raw_model_event.type === 'raw_server_event' &&
                    event.raw_model_event.transcript) {
                    console.log('Transcript received in raw_model_event, stopping playback');
                    this.stopAudioPlayback();
                }
                break;
            case 'audio_start':
                this.ignoreIncomingAudio = false;
                this.isPlayingAudio = true;
                this.updateStatus('Speaking...', 'speaking');
                console.log('New audio stream started, enabling playback');
                break;
            case 'client.speech_started':
                this.updateStatus('Listening...', 'listening');
                this.stopAudioPlayback();
                break;
            case 'playback.drained':
                if (this.isConnected && !this.isPlayingAudio) {
                    this.updateStatus('Connected', 'connected');
                }
                break;
            case 'response.audio.done':
                // Do not update status here; wait for playback 'drained'
                break;
            case 'audio_interrupted':
            case 'input_audio_buffer.speech_started':
                console.log('Interruption event received:', event.type);
                this.updateStatus('Listening...', 'listening');
                this.stopAudioPlayback();
                break;
            case 'metrics':
                this.updateMetrics(event.data);
                break;
        }
    }

    updateMetrics(data) {
        if (!data) return;

        const updateField = (id, value, isCost = false) => {
            const el = document.getElementById(id);
            if (el && value !== undefined) {
                if (isCost) {
                    el.textContent = '$' + value;
                } else if (id.includes('token')) {
                    el.textContent = value;
                } else {
                    el.textContent = value + ' ms';
                }

                // Trigger animation
                el.classList.remove('metric-updated');
                void el.offsetWidth; // trigger reflow
                el.classList.add('metric-updated');

                // Remove class after animation
                setTimeout(() => {
                    el.classList.remove('metric-updated');
                }, 500);
            }
        };

        if (data.tts) updateField('metric-tts', data.tts);
        if (data.llm) updateField('metric-llm', data.llm);
        if (data.stt) updateField('metric-stt', data.stt);
        if (data.turn) updateField('metric-turn', data.turn);
        if (data.input_tokens !== undefined) updateField('metric-input_tokens', data.input_tokens);
        if (data.output_tokens !== undefined) updateField('metric-output_tokens', data.output_tokens);
        if (data.cost) updateField('metric-cost', data.cost, true);
    }
    updateLastMessageFromHistory(history) {
        if (!history || !Array.isArray(history) || history.length === 0) return;
        // Find the last message item in history
        let last = null;
        for (let i = history.length - 1; i >= 0; i--) {
            const it = history[i];
            if (it && it.type === 'message') { last = it; break; }
        }
        if (!last) return;
        const itemId = last.item_id;

        // Extract a text representation (for assistant transcript updates)
        let text = '';
        if (Array.isArray(last.content)) {
            for (const part of last.content) {
                if (!part || typeof part !== 'object') continue;
                if (part.type === 'text' && part.text) text += part.text;
                else if (part.type === 'input_text' && part.text) text += part.text;
                else if ((part.type === 'input_audio' || part.type === 'audio') && part.transcript) text += part.transcript;
            }
        }

        const node = this.messageNodes.get(itemId);
        if (!node) {
            // If we haven't rendered this item yet, append it now.
            this.addMessageFromItem(last);
            return;
        }

        // Update only the text content of the bubble, preserving any images already present.
        const bubble = node.querySelector('.message-bubble');
        if (bubble && text && text.trim()) {
            // If there's an <img>, keep it and only update the trailing caption/text node.
            const hasImg = !!bubble.querySelector('img');
            if (hasImg) {
                // Ensure there is a caption div after the image
                let cap = bubble.querySelector('.image-caption');
                if (!cap) {
                    cap = document.createElement('div');
                    cap.className = 'image-caption';
                    cap.style.marginTop = '0.5rem';
                    bubble.appendChild(cap);
                }
                cap.textContent = text.trim();
            } else {
                bubble.textContent = text.trim();
            }
            this.scrollToBottom();
        }
    }

    syncMissingFromHistory(history) {
        if (!history || !Array.isArray(history)) return;
        for (const item of history) {
            if (!item || item.type !== 'message') continue;
            const id = item.item_id;
            if (!id) continue;
            if (!this.seenItemIds.has(id)) {
                this.addMessageFromItem(item);
            }
        }
    }

    addMessageFromItem(item) {
        try {
            if (!item || item.type !== 'message') return;
            const role = item.role;
            let content = '';
            let imageUrls = [];

            if (Array.isArray(item.content)) {
                for (const contentPart of item.content) {
                    if (!contentPart || typeof contentPart !== 'object') continue;
                    if (contentPart.type === 'text' && contentPart.text) {
                        content += contentPart.text;
                    } else if (contentPart.type === 'input_text' && contentPart.text) {
                        content += contentPart.text;
                    } else if (contentPart.type === 'input_audio' && contentPart.transcript) {
                        content += contentPart.transcript;
                    } else if (contentPart.type === 'audio' && contentPart.transcript) {
                        content += contentPart.transcript;
                    } else if (contentPart.type === 'input_image') {
                        const url = contentPart.image_url || contentPart.url;
                        if (typeof url === 'string' && url) imageUrls.push(url);
                    }
                }
            }

            let node = null;
            if (imageUrls.length > 0) {
                for (const url of imageUrls) {
                    node = this.addImageMessage(role, url, content.trim());
                }
            } else if (content && content.trim()) {
                node = this.addMessage(role, content.trim());
            }
            if (node && item.item_id) {
                this.messageNodes.set(item.item_id, node);
                this.seenItemIds.add(item.item_id);
            }
        } catch (e) {
            console.error('Failed to add message from item:', e, item);
        }
    }

    addMessage(type, content) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${type}`;

        const bubbleDiv = document.createElement('div');
        bubbleDiv.className = 'message-bubble';
        bubbleDiv.textContent = content;

        messageDiv.appendChild(bubbleDiv);
        this.messagesContent.appendChild(messageDiv);
        this.scrollToBottom();

        return messageDiv;
    }

    addImageMessage(role, imageUrl, caption = '') {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${role}`;

        const bubbleDiv = document.createElement('div');
        bubbleDiv.className = 'message-bubble';

        const img = document.createElement('img');
        img.src = imageUrl;
        img.alt = 'Uploaded image';
        img.style.maxWidth = '220px';
        img.style.borderRadius = '8px';
        img.style.display = 'block';

        bubbleDiv.appendChild(img);
        if (caption) {
            const cap = document.createElement('div');
            cap.textContent = caption;
            cap.style.marginTop = '0.5rem';
            bubbleDiv.appendChild(cap);
        }

        messageDiv.appendChild(bubbleDiv);
        this.messagesContent.appendChild(messageDiv);
        this.scrollToBottom();

        return messageDiv;
    }

    addUserImageMessage(imageUrl, caption = '') {
        return this.addImageMessage('user', imageUrl, caption);
    }

    addRawEvent(event) {
        const eventDiv = document.createElement('div');
        eventDiv.className = 'event';

        const headerDiv = document.createElement('div');
        headerDiv.className = 'event-header';
        headerDiv.innerHTML = `
            <span>${event.type}</span>
            <span>▼</span>
        `;

        const contentDiv = document.createElement('div');
        contentDiv.className = 'event-content collapsed';
        contentDiv.textContent = JSON.stringify(event, null, 2);

        headerDiv.addEventListener('click', () => {
            const isCollapsed = contentDiv.classList.contains('collapsed');
            contentDiv.classList.toggle('collapsed');
            headerDiv.querySelector('span:last-child').textContent = isCollapsed ? '▲' : '▼';
        });

        eventDiv.appendChild(headerDiv);
        eventDiv.appendChild(contentDiv);
        this.eventsContent.appendChild(eventDiv);

        // Auto-scroll events pane
        this.eventsContent.scrollTop = this.eventsContent.scrollHeight;
    }

    addToolEvent(event) {
        const eventDiv = document.createElement('div');
        eventDiv.className = 'event';

        let title = '';
        let description = '';
        let eventClass = '';

        if (event.type === 'handoff') {
            title = `🔄 Handoff`;
            description = `From ${event.from} to ${event.to}`;
            eventClass = 'handoff';
        } else if (event.type === 'tool_start') {
            title = `🔧 Tool Started`;
            description = `Running ${event.tool}`;
            eventClass = 'tool';
        } else if (event.type === 'tool_end') {
            title = `✅ Tool Completed`;
            description = `${event.tool}: ${event.output || 'No output'}`;
            eventClass = 'tool';
        }

        eventDiv.innerHTML = `
            <div class="event-header ${eventClass}">
                <div>
                    <div style="font-weight: 600; margin-bottom: 2px;">${title}</div>
                    <div style="font-size: 0.8rem; opacity: 0.8;">${description}</div>
                </div>
                <span style="font-size: 0.7rem; opacity: 0.6;">${new Date().toLocaleTimeString()}</span>
            </div>
        `;

        this.toolsContent.appendChild(eventDiv);

        // Auto-scroll tools pane
        this.toolsContent.scrollTop = this.toolsContent.scrollHeight;
    }

    async playAudio(audioBase64) {
        try {
            if (!audioBase64 || audioBase64.length === 0) {
                console.warn('Received empty audio data, skipping playback');
                return;
            }

            const int16Array = this.decodeBase64ToInt16(audioBase64);
            if (!int16Array || int16Array.length === 0) {
                console.warn('Audio chunk has no samples, skipping');
                return;
            }

            await this.playAudioRaw(int16Array);

        } catch (error) {
            console.error('Failed to play audio:', error);
            this.pendingPlaybackChunks = [];
        }
    }

    async playAudioRaw(int16Array) {
        try {
            this.pendingPlaybackChunks.push(int16Array);
            await this.ensurePlaybackNode();
            this.flushPendingPlaybackChunks();
        } catch (error) {
            console.error('Failed to play raw audio:', error);
            this.pendingPlaybackChunks = [];
        }
    }

    async ensurePlaybackNode() {
        if (this.playbackNode) {
            return;
        }

        if (!this.playbackInitPromise) {
            this.playbackInitPromise = (async () => {
                if (!this.playbackAudioContext) {
                    this.playbackAudioContext = new AudioContext({ sampleRate: 24000, latencyHint: 'interactive' });
                }

                if (this.playbackAudioContext.state === 'suspended') {
                    try { await this.playbackAudioContext.resume(); } catch { }
                }

                if (!this.playbackAudioContext.audioWorklet) {
                    throw new Error('AudioWorklet API not supported in this browser.');
                }

                await this.playbackAudioContext.audioWorklet.addModule('audio-playback.worklet.js');

                this.playbackNode = new AudioWorkletNode(this.playbackAudioContext, 'pcm-playback', { outputChannelCount: [1] });
                this.playbackNode.port.onmessage = (event) => {
                    const message = event.data;
                    if (!message || typeof message !== 'object') return;
                    if (message.type === 'drained') {
                        this.isPlayingAudio = false;
                        this.handleRealtimeEvent({ type: 'playback.drained' });
                    }
                };

                // Provide initial configuration for fades.
                const fadeSamples = Math.floor(this.playbackAudioContext.sampleRate * this.playbackFadeSec);
                this.playbackNode.port.postMessage({ type: 'config', fadeSamples });

                this.playbackNode.connect(this.playbackAudioContext.destination);
            })().catch((error) => {
                this.playbackInitPromise = null;
                throw error;
            });
        }

        await this.playbackInitPromise;
    }

    flushPendingPlaybackChunks() {
        if (!this.playbackNode) {
            return;
        }

        while (this.pendingPlaybackChunks.length > 0) {
            const chunk = this.pendingPlaybackChunks.shift();
            if (!chunk || !(chunk instanceof Int16Array) || chunk.length === 0) {
                continue;
            }

            try {
                this.playbackNode.port.postMessage(
                    { type: 'chunk', payload: chunk.buffer },
                    [chunk.buffer]
                );
                this.isPlayingAudio = true;
            } catch (error) {
                console.error('Failed to enqueue audio chunk to worklet:', error);
            }
        }
    }

    decodeBase64ToInt16(audioBase64) {
        try {
            const binaryString = atob(audioBase64);
            const length = binaryString.length;
            const bytes = new Uint8Array(length);
            for (let i = 0; i < length; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }
            return new Int16Array(bytes.buffer);
        } catch (error) {
            console.error('Failed to decode audio chunk:', error);
            return null;
        }
    }

    stopAudioPlayback() {
        console.log('Stopping audio playback due to interruption');

        // Flag to discard any in-flight audio chunks from the cancelled stream
        this.ignoreIncomingAudio = true;

        this.pendingPlaybackChunks = [];

        if (this.playbackNode) {
            try {
                this.playbackNode.port.postMessage({ type: 'stop' });
            } catch (error) {
                console.error('Failed to notify playback worklet to stop:', error);
            }
        }

        this.isPlayingAudio = false;

        console.log('Audio playback stopped and queue cleared');
    }

    scrollToBottom() {
        this.messagesContent.scrollTop = this.messagesContent.scrollHeight;
    }
}

// Initialize the demo when the page loads
document.addEventListener('DOMContentLoaded', () => {
    new RealtimeDemo();
});
