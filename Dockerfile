FROM python:3.11-slim

# 7860 은 흔히 쓰는 컨테이너 포트다. 호스팅이 PORT 를 주면 그걸 따른다.
ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     PORT=7860     ONNX_MODEL_DIR=/app/models_onnx     OCR_ENGINE=rapid     # 무료 티어는 0.1~0.5 vCPU 다. 스레드를 늘리면 경쟁만 는다.
    # 실측에서 24스레드(25.8s)가 1스레드(29.8s)와 비슷했고 4스레드가 12.8s 였다.
    # 코어가 많은 곳에 올리면 이 값을 올린다.
    OMP_NUM_THREADS=1     OCR_THREADS=1

# opencv 가 libGL 을 링크한다. slim 이미지에는 없다.
RUN apt-get update && apt-get install -y --no-install-recommends         libgl1 libglib2.0-0 libgomp1     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성을 먼저 복사해서 레이어 캐시를 살린다.
COPY requirements.txt requirements-ocr.txt ./
RUN pip install --no-cache-dir -r requirements.txt     && pip install --no-cache-dir -r requirements-ocr.txt

COPY core/ core/
COPY templates/ templates/
COPY static/ static/
COPY models_onnx/ models_onnx/
COPY data/dur.db data/dur.db
COPY main.py .

# 루트로 돌리지 않는다. dur.db 는 읽기 전용으로만 연다.
RUN useradd -m -u 1000 app && chown -R app:app /app
USER app
ENV HOME=/home/app

# 기동 시 모델을 올리고 실제 추론을 한 번 돌린다. 90초를 준다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3   CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/health').read()"

EXPOSE 7860
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
