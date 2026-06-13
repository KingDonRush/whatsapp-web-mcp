FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WHATSAPP_MCP_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

WORKDIR /app
COPY pyproject.toml README.md LICENSE server.py ./
COPY whatsapp_web_mcp ./whatsapp_web_mcp
RUN pip install --no-cache-dir .

VOLUME ["/data"]
USER appuser
ENTRYPOINT ["whatsapp-web-mcp"]
