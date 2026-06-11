FROM python:3.12-slim

WORKDIR /app

# ripgrep powers the fast path of the search_files tool on every container
# arch (the PyPI ripgrep wheel only covers linux x86_64).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency manifest first for layer caching
COPY pyproject.toml ./
COPY README.md ./

# Install dependencies (voice-input deps are darwin-only via platform marker)
RUN uv pip install --system --no-cache -e .

# Copy source
COPY . .

# Running as root is intentional: docker-compose mounts ${HOME}:${HOME} so the
# container must share the host user's UID to write to those files. If you do
# NOT use the home-directory mount, add --user $(id -u):$(id -g) at runtime.
ENV NORTH_HOME=/data
EXPOSE 8000

# Liveness probe — /health returns 200 with no authentication.
# --start-period gives the lifespan startup time before failures count.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "-m", "uvicorn", "orchestrator.app:app", \
     "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
