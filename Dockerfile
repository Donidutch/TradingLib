# TradingLib API server with both research consoles.
#
#   docker build -t tradinglib .
#   docker run --rm -p 127.0.0.1:8200:8200 tradinglib
#
# Two stages: Node builds the React console, and only its static output is
# copied into the Python image, so the final image carries no Node toolchain.

# --- Stage 1: build the React console (web/ -> web/dist) ---------------------
FROM node:24-slim AS web
WORKDIR /web
# Dependencies first: this layer is reused until package-lock.json changes.
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- Stage 2: install the Python environment --------------------------------
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Install the locked dependencies before copying the source, so a code change
# does not reinstall every package.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-install-project --no-editable
COPY tradinglib/ ./tradinglib/
RUN uv sync --locked --no-editable --compile-bytecode

# --- Stage 3: the runtime image -------------------------------------------------
# Only the virtual environment, the static consoles and Python itself; no uv,
# no Node, no build caches.
FROM python:3.14-slim AS app
RUN useradd --create-home --uid 10001 tradinglib
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY ui/ ./ui/
COPY --from=web /web/dist ./web/dist
# Only the data directory is writable by the service user.
RUN mkdir -p /app/data && chown tradinglib:tradinglib /app/data
USER tradinglib

# The package is installed into the venv (not editable), so point it at the
# consoles and the data directory explicitly.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TRADINGLIB_DATA_DIR=/app/data \
    TRADINGLIB_UI_DIR=/app/ui \
    TRADINGLIB_WEB_DIR=/app/web/dist
VOLUME ["/app/data"]
EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8200/api/health', timeout=4)"]

# python -m tradinglib.api pins the asyncio event loop (see tradinglib/api/__main__.py).
# 0.0.0.0 is needed inside a container; publish the port on 127.0.0.1 only.
CMD ["python", "-m", "tradinglib.api", "--host", "0.0.0.0", "--port", "8200"]
