# ============================================================
# LIVE MOVING OBJECT DETECTION USING YOLOv11 — RAILWAY DOCKERFILE
# ============================================================
FROM python:3.11-slim

# System libraries required by OpenCV / Ultralytics at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project (app.py, templates/, static/, yolo11n.pt)
COPY . .

# Ensure runtime folders exist even if .gitignore stripped them
RUN mkdir -p output screenshots

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD gunicorn app:app --workers 1 --threads 8 --timeout 180 --bind 0.0.0.0:${PORT:-8080}