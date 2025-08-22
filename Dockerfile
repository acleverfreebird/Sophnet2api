# Multi-stage build to reduce final image size
FROM python:3.12-slim AS builder

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Final stage
FROM python:3.12-slim

WORKDIR /app

# Install minimal runtime dependencies for playwright (only essential ones)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libxrandr2 \
    libasound2 \
    libpangocairo-1.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxfixes3 \
    libgbm1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /tmp/* /var/tmp/*

# Copy Python packages from builder stage
COPY --from=builder /root/.local /root/.local

# Install playwright chromium browser and clean up
RUN PATH=/root/.local/bin:$PATH playwright install chromium && \
    rm -rf /tmp/* /var/tmp/*

# Copy application files
COPY . /app

# Make sure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH

CMD ["python", "main.py"]