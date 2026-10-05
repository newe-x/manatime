FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Tela virtual com VNC embutido (TigerVNC) + noVNC para fazer login nas contas pelo navegador do host
RUN apt-get update && apt-get install -y --no-install-recommends \
        tigervnc-standalone-server tigervnc-tools fluxbox novnc websockify \
        fonts-noto fonts-noto-color-emoji tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000 6080
ENTRYPOINT ["/entrypoint.sh"]
