FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir "mitmproxy==12.2.3" "dnslib==0.9.25" "warcio==1.7.5" "httpx==0.28.1" "beautifulsoup4==4.13.4"
WORKDIR /app
COPY frozen/ /app/frozen/
COPY frozen_addon.py join.sh selftest.sh start-gateway.sh /app/
ENV PYTHONPATH=/app FROZEN_DATA_DIR=/data
VOLUME /data
