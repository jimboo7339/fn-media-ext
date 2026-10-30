FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data/app \
    TRIM_DB_DIR=/data/trim \
    TRIM_IMG_ROOT=/data/meta/img

WORKDIR /app

# 日报卡片要用 Pillow 画中文；python:3.12-slim 里没有中文字体。
# 选 wqy-microhei 是因为它只有几 MB，Noto CJK 要 50MB+。
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-wqy-microhei \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY static ./static

RUN mkdir -p /data/app

EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--no-access-log"]
