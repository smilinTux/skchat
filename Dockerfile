FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy skchat source
COPY . /app/skchat/

# Install skchat with CLI extras + web UI deps
RUN pip install --no-cache-dir \
    /app/skchat/[cli] \
    fastapi \
    uvicorn[standard] \
    httpx \
    websockets \
    python-multipart

# Bind to all interfaces inside the container
ENV SKCHAT_HOST=0.0.0.0

# Voice streaming env vars (GPU services on .100, LLM proxy on gateway host)
ENV SKCHAT_STT_URL=http://192.168.0.100:18794/v1/audio/transcriptions
ENV SKCHAT_TTS_URL=http://192.168.0.100:18793/audio/speech
ENV SKCHAT_LLM_URL=http://192.168.0.100:11434/v1/chat/completions
ENV SKCHAT_LLM_MODEL=qwen3.5:9b
ENV SKCHAT_VOICE_LLM_URL=http://192.168.0.158:18795/voice-llm
ENV SKCHAT_USE_OPENCLAW=true

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8765/health || exit 1

CMD ["skchat", "webui", "--port", "8765", "--no-browser"]
