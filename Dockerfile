# ---- Job Control Center : one image, three roles (API / crawler / dashboard) ----
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Python deps first for better layer caching. fastapi/lxml/pydantic/anthropic all
# ship manylinux wheels, so no compiler toolchain is needed on -slim.
COPY backend/requirements.txt backend/requirements.txt
COPY dashboard/requirements.txt dashboard/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt \
 && pip install --no-cache-dir -r dashboard/requirements.txt

# App source. .dockerignore keeps the DB / venv / logs out of the image.
COPY . .

# Persistent DB lives in a subdir that compose mounts as a volume; seed CSVs in
# backend/data/ stay baked into the image (not hidden by the volume mount).
RUN mkdir -p backend/data/db logs

EXPOSE 8000 8501
