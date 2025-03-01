# Stage 1: Build stage
FROM python:3.13.1-slim-bookworm AS builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    python3-dev \
    libmariadb3 \
    libmariadb-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file and install dependencies
COPY app/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install --prefix=/install -r requirements.txt

# Stage 2: Final stage
FROM python:3.13.1-slim-bookworm

WORKDIR /app

# Install curl in the final stage
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy only the necessary files from the build stage
COPY --from=builder /install /usr/local
COPY app /app

# Command to run the application
CMD ["python", "main.py"]

# Health check
HEALTHCHECK --interval=5s --timeout=2s --start-period=5s --retries=1 CMD python /app/health_check.py