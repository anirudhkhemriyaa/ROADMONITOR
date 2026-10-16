# Road Monitor backend image (used with docker-compose.yml at the repo root).
# Phase B note: the API service itself still runs the SQLite store; the db and
# redis services below are the Phase C infrastructure (PostGIS + worker queue).
FROM python:3.12-slim

WORKDIR /srv/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
CMD ["uvicorn", "roadmonitor.main:app", "--host", "0.0.0.0", "--port", "8000"]
