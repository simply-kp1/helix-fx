FROM python:3.12-slim

# Runtime env
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    HOST=0.0.0.0 \
    HELIX_DATA_DIR=/data \
    TZ=Europe/London

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create the data directory (Fly volume mounts here at runtime)
RUN mkdir -p /data/logs /data/backtest_cache

EXPOSE 8080

CMD ["python", "run_dashboard.py"]
