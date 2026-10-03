FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    PYTHONPATH=/app/backend \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/backend/.venv/bin:${PATH}"

WORKDIR /app/backend

RUN pip install --no-cache-dir uv

# Dependencies first so code edits do not reinstall them.
COPY backend/pyproject.toml .
RUN uv sync --no-cache --no-install-project --extra dev

# Chromium for the LinkedIn channel (works on x86_64 and ARM, e.g. Oracle Ampere A1).
RUN playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

COPY backend/app/ ./app/
COPY backend/tests/ ./tests/
COPY search_queries.yaml ./search_queries.yaml

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
