class VADManager {
    constructor() {
        this.vadMatcher = null;
        this.onSpeechStart = () => { };
        this.onSpeechEnd = () => { };
        this.isListening = false;
    }

    async initialize(onSpeechStart, onSpeechEnd) {
        this.onSpeechStart = onSpeechStart;
        this.onSpeechEnd = onSpeechEnd;
        // Pre-loading logic could be here if we want to fetch the model early,
        // but MicVAD.new wraps model loading and stream handling.
        console.log("VAD Manager initialized (waiting for stream)");
        return true;
    }

    async start(stream) {
        if (this.vadMatcher) {
            // Already started?
            this.vadMatcher.start();
            this.isListening = true;
            return;
        }

        if (!stream) {
            console.error("VAD start requires a media stream");
            return;
        }

        try {
            console.log("Starting VAD with existing stream...");
            this.vadMatcher = await vad.MicVAD.new({
                stream: stream,
                onSpeechStart: () => {
                    console.log("VAD: Speech Started");
                    if (this.onSpeechStart) this.onSpeechStart();
                },
                onSpeechEnd: (audio) => {
                    console.log("VAD: Speech Ended");
                    if (this.onSpeechEnd) this.onSpeechEnd(audio);
                },
                positiveSpeechThreshold: 0.5,
                negativeSpeechThreshold: 0.35,
            });

            this.vadMatcher.start();
            this.isListening = true;
            console.log("VAD Started");
        } catch (e) {
            console.error("Failed to start VAD:", e);
        }
    }

    stop() {
        if (this.vadMatcher) {
            this.vadMatcher.pause();
            // We don't destroy it fully because we might want to resume,
            // but if the stream ends, we might need to recreate.
            // For now, pause is fine.
            this.isListening = false;
        }
    }
}
