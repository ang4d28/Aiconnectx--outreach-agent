FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1

WORKDIR /app/backend

RUN pip install --no-cache-dir uv

COPY backend/pyproject.toml .
RUN uv sync --no-cache

COPY backend/app/ ./app/
COPY backend/tests/ ./tests/

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
