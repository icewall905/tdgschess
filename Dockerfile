FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends stockfish libcairo2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8000
# long polls hold connections open: stop waiting for them after 3 s so the shutdown hook (hibernate games) runs
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "3"]
