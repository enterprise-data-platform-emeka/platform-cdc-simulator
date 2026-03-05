# ── Build stage ───────────────────────────────────────────────────────────────
# We use a two-stage build:
# 1. "builder" installs dependencies into a separate layer.
# 2. The final image copies only what it needs from the builder.
# This keeps the final image small and avoids shipping build tools to production.

FROM python:3.11.8-slim AS builder

WORKDIR /build

# Install dependencies into a local directory so we can copy them cleanly.
# --no-cache-dir keeps the image smaller by not caching downloaded packages.
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Final image ───────────────────────────────────────────────────────────────

FROM python:3.11.8-slim

# Non-root user for security — never run application code as root in a container.
RUN useradd --create-home --shell /bin/bash simulator
USER simulator
WORKDIR /home/simulator/app

# Copy installed packages from the builder stage
COPY --from=builder /install /usr/local

# Copy application source
COPY --chown=simulator:simulator simulator/ ./simulator/
COPY --chown=simulator:simulator main.py .

# ENVIRONMENT must be provided at runtime via docker run -e or docker compose.
# We do not set a default here because running in the wrong environment
# (e.g. prod config against a dev database) would be a silent misconfiguration.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default command: simulate. Override with "schema", "seed", or "reset" as needed.
# Example: docker run cdc-simulator:latest seed
ENTRYPOINT ["python", "main.py"]
CMD ["simulate"]
