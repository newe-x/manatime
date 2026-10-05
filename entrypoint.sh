#!/usr/bin/env bash
set -euo pipefail

: "${SCREEN_GEOMETRY:=1440x900}"
: "${VNC_PASSWORD:?Defina VNC_PASSWORD no .env}"

# Perfis antigos podem deixar locks se o container morreu sem fechar o Chromium
find /data/profiles -maxdepth 2 -name 'Singleton*' -delete 2>/dev/null || true
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

mkdir -p /root/.vnc
echo "$VNC_PASSWORD" | vncpasswd -f > /root/.vnc/passwd
chmod 600 /root/.vnc/passwd

# Xvnc é ao mesmo tempo a tela :99 e o servidor VNC (só localhost; o noVNC faz a ponte)
Xvnc :99 -geometry "$SCREEN_GEOMETRY" -depth 24 -rfbport 5900 -localhost \
    -SecurityTypes VncAuth -PasswordFile /root/.vnc/passwd -AlwaysShared -nolisten tcp &
for _ in $(seq 1 50); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.1; done
fluxbox >/dev/null 2>&1 &

websockify --web /usr/share/novnc 6080 127.0.0.1:5900 >/dev/null 2>&1 &

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
