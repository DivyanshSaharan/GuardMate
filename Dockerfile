# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend-build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./frontend/
RUN npm run build

FROM python:3.13-slim-bookworm AS python-build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/requirements.txt backend/requirements-ai.txt backend/requirements-voice.txt ./backend/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install \
        -r backend/requirements-ai.txt -r backend/requirements-voice.txt

FROM python:3.13-slim-bookworm AS speech-build
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates cmake build-essential \
    && rm -rf /var/lib/apt/lists/*
# Official whisper.cpp v1.8.2 release commit. Verify the source before compiling it.
ADD --checksum=sha256:ec8e33f4c838ed459ad0a23da255c0262f150a6f54839a17e504ddf1495305d5 \
    https://codeload.github.com/ggml-org/whisper.cpp/tar.gz/4979e04f5dcaccb36057e059bbaed8a2f5288315 \
    /tmp/whisper-source.tar.gz
COPY backend/scripts/setup_speech.py ./backend/scripts/setup_speech.py
# These immutable downloads verify both byte size and SHA-256 and preserve licences.
RUN python backend/scripts/setup_speech.py --download --models-only
RUN mkdir /tmp/whisper-source \
    && tar -xzf /tmp/whisper-source.tar.gz -C /tmp/whisper-source --strip-components=1 \
    && cmake -S /tmp/whisper-source -B /tmp/whisper-build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_NATIVE=OFF \
        -DGGML_OPENMP=OFF \
        -DWHISPER_BUILD_TESTS=OFF \
        -DWHISPER_BUILD_SERVER=OFF \
        -DWHISPER_CURL=OFF \
    && cmake --build /tmp/whisper-build --parallel 2 --target whisper-cli quantize \
    && mkdir -p /opt/whisper/bin \
    && cp /tmp/whisper-build/bin/whisper-cli /opt/whisper/bin/whisper-cli \
    && /tmp/whisper-build/bin/quantize \
        models/speech/ggml-base.en.bin models/speech/ggml-base.en-q5_0.bin q5_0 \
    && rm models/speech/ggml-base.en.bin \
    && /opt/whisper/bin/whisper-cli --help

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    MALLOC_ARENA_MAX=2 \
    GUARDMATE_DATA_DIR=/app/.data \
    HF_HOME=/app/.cache/huggingface \
    GUARDMATE_WHISPER_CLI=/opt/whisper/bin/whisper-cli \
    GUARDMATE_WHISPER_MODEL=/app/models/speech/ggml-base.en-q5_0.bin \
    GUARDMATE_PIPER_MODEL=/app/models/speech/en_US-ljspeech-high.onnx
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 guardmate \
    && useradd --uid 10001 --gid guardmate --home-dir /app --no-create-home guardmate \
    && mkdir -p /app/.data /app/.cache/huggingface \
    && chown -R guardmate:guardmate /app/.data /app/.cache
COPY --from=python-build /opt/venv/ /opt/venv/
COPY --from=speech-build /opt/whisper/ /opt/whisper/
COPY --from=speech-build /app/models/speech/ /app/models/speech/
COPY --from=speech-build /app/.cache/voice/licenses/ /app/speech-licenses/
COPY --from=frontend-build /app/dist/ /app/dist/
COPY backend/guardmate/ ./backend/guardmate/
COPY backend/scripts/smoke_speech.py ./backend/scripts/smoke_speech.py
USER guardmate:guardmate
# Exercise the Linux runtimes and writable temporary files as the production user.
# Only synthetic Piper audio is used; no hosted model request or recording is made.
RUN python backend/scripts/smoke_speech.py --run
EXPOSE 10000
# One worker keeps the application lock and native speech concurrency bound effective.
CMD ["sh", "-c", "exec python -m uvicorn guardmate.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers --forwarded-allow-ips '*' --no-access-log"]
