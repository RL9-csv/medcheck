FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# 의존성을 먼저 복사해서 레이어 캐시를 살린다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ core/
COPY templates/ templates/
COPY static/ static/
COPY data/dur.db data/dur.db
COPY main.py .

# 루트로 돌리지 않는다. dur.db는 읽기 전용으로만 연다.
RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

# 컨테이너 자체 헬스체크. Railway의 healthcheckPath와 별개로 동작한다.
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/health').read()"

EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
