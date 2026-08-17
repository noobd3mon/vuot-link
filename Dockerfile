# Repo: tool tự vượt link cuty.io — deploy Railway (hoặc bất kỳ container Linux nào).
#
# Cốt lõi: Cloudflare Turnstile KHÔNG pass headless → phải chạy Chrome HEADED.
# Trên server không màn hình → dùng Xvfb (virtual display) + headless=False.
# Lệnh: Xvfb :99 & DISPLAY=:99 → chromium headed render vào Xvfb → Turnstile pass.

FROM python:3.11-slim

# Chromium + Xvfb (display ảo cho headed) + x11vnc + novnc (manual mode remote
# qua web, cùng port với app) + fonts + dbus (Chrome cần) + tini (reap zombie Chrome).
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        xvfb \
        xauth \
        x11vnc \
        novnc \
        fonts-liberation \
        fonts-noto-cjk \
        dbus \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Tool dùng chromium hệ thống (qua subprocess + CDP), không cần `playwright install`.
ENV CHROME_PATH=/usr/bin/chromium
ENV PROFILE_DIR=/data/profile
ENV DISPLAY=:99
# noVNC cho manual mode remote (web page mở /vnc/ để điều khiển Chrome).
ENV NOVNC=1
ENV NOVNC_DIR=/usr/share/novnc
ENV VNC_HOST=127.0.0.1
ENV VNC_PORT=5900
# Python thoát ngay khi có log (Railway thấy log ngay).
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vuot_link.py app.py start.sh ./
RUN chmod +x start.sh

RUN mkdir -p /data/profile

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8080')+'/health').read()" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# start.sh: đợi Xvfb sẵn sàng → x11vnc → uvicorn (cổng Railway cấp $PORT).
CMD ["/app/start.sh"]
