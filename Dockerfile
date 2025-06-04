# Stage 1: Builder stage for installing dependencies
FROM python:3.13-slim-bookworm AS builder

WORKDIR /app

# Install build essentials needed for some python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Final stage
FROM python:3.13-slim-bookworm AS final

WORKDIR /app

# Copy virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv

# Activate the virtual environment
ENV PATH="/opt/venv/bin:$PATH"

# Copy the application script
COPY main.py .
# config.json will be mounted via docker-compose, so it's not copied here.
# The data directory will also be managed by a Docker volume.

# Set the entrypoint for the container
# The script will be run directly. If it needs to be executable: RUN chmod +x main.py
CMD ["python", "main.py"]