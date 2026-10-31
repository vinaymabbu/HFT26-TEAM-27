# Backend (FastAPI + OpenCV) for Render / Railway / Fly. The dashboard is deployed separately (Vercel).
FROM python:3.13-slim
WORKDIR /app
COPY src/backend/requirements.txt src/backend/requirements.txt
RUN pip install --no-cache-dir -r src/backend/requirements.txt
COPY src/backend src/backend
COPY fixtures fixtures
COPY data data
ENV STORAGE_DIR=/data PYTHONUNBUFFERED=1
WORKDIR /app/src/backend
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
