FROM python:3.11-slim

# 7860 은 Hugging Face Spaces 의 기본 포트다. README 의 app_port 와 맞춘다.
ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     PORT=7860     PADDLE_MODEL_DIR=/app/models     PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python     OCR_ENGINE=paddle

# opencv 가 libGL 을 링크한다. slim 이미지에는 없다.
RUN apt-get update && apt-get install -y --no-install-recommends         libgl1 libglib2.0-0 libgomp1     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성을 먼저 복사해서 레이어 캐시를 살린다. OCR 은 무거워서 따로 둔다.
COPY requirements.txt requirements-ocr.txt ./
RUN pip install --no-cache-dir -r requirements.txt     && pip install --no-cache-dir -r requirements-ocr.txt

COPY core/ core/
COPY templates/ templates/
COPY static/ static/
COPY models/ models/
COPY data/dur.db data/dur.db
COPY main.py .

# 루트로 돌리지 않는다. dur.db 는 읽기 전용으로만 연다.
# paddle 이 홈 디렉토리에 캐시를 쓰므로 app 사용자의 홈이 필요하다.
RUN useradd -m -u 1000 app && chown -R app:app /app
USER app
ENV HOME=/home/app

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3   CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/health').read()"

EXPOSE 7860
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
