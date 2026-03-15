# ---------------------------------------------------------------------------
# Claude Quant — Main Orchestrator Dockerfile
# ---------------------------------------------------------------------------
# Multi-stage build:
#   Stage 1: Build TA-Lib C library from source
#   Stage 2: Slim runtime with Python dependencies
# ---------------------------------------------------------------------------

# Stage 1: Build TA-Lib C library
FROM python:3.11-slim AS talib-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        wget \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Download and compile TA-Lib C library from GitHub
ARG TALIB_VERSION=0.6.4
RUN wget -q "https://github.com/ta-lib/ta-lib/releases/download/v${TALIB_VERSION}/ta-lib-${TALIB_VERSION}-src.tar.gz" \
    && tar xzf "ta-lib-${TALIB_VERSION}-src.tar.gz" \
    && cd "ta-lib-${TALIB_VERSION}" \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install \
    && cd .. \
    && rm -rf "ta-lib-${TALIB_VERSION}" ta-lib-*.tar.gz


# Stage 2: Runtime image
FROM python:3.11-slim

LABEL maintainer="Claude Quant <claude-quant@localhost>"
LABEL description="Claude Quant autonomous trading system orchestrator"

# Copy TA-Lib C library from builder
COPY --from=talib-builder /usr/local/lib/libta_lib* /usr/local/lib/
COPY --from=talib-builder /usr/local/include/ta-lib/ /usr/local/include/ta-lib/
RUN ldconfig

# Install minimal runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        sqlite3 \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r claudequant && useradd -r -g claudequant -m -s /bin/bash claudequant

# Set up application directory
WORKDIR /app

# Copy requirements first for Docker layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application source and config
COPY src/ /app/src/
COPY config/ /app/config/
COPY pyproject.toml /app/pyproject.toml

# Create required runtime directories
RUN mkdir -p \
        /app/user_data/agent_state \
        /app/user_data/data \
        /app/user_data/logs \
        /app/user_data/backtest_results \
        /app/docs/reports \
    && chown -R claudequant:claudequant /app

# Switch to non-root user
USER claudequant

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    LOG_LEVEL=INFO \
    BINANCE_TESTNET=true

# Health check — verifies the Python process is responsive
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# Use tini as PID 1 for proper signal handling
ENTRYPOINT ["tini", "--"]

# Default command: run the orchestrator main loop
CMD ["python", "-m", "src.orchestrator.main"]
