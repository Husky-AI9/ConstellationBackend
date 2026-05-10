# ─── Build Stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --no-cache-dir --target=/install -r requirements.txt

# ─── Runtime Stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Team USA Hometown Signals API"
LABEL org.opencontainers.image.description="FastAPI backend for Team USA Hackathon Hub — Hometown Signals & LA28 Momentum"
LABEL org.opencontainers.image.version="1.0.0"

# Non-root user for security
RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local/lib/python3.12/site-packages/

# Copy application source
COPY app/ app/

# Cloud Run injects PORT env var; default to 8080
ENV PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    # CORS — override in Cloud Run environment variables as needed
    CORS_ALLOW_ALL_ORIGINS=false

# Expose the port (documentation only; Cloud Run uses $PORT)
EXPOSE 8080

# Switch to non-root
USER appuser

# Start Uvicorn through the Python module so Cloud Run does not depend on
# console scripts being copied into PATH from the builder stage.
# Bind to $PORT as required by Cloud Run.
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --log-level info"]
