FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency manifest first for layer caching
COPY pyproject.toml ./
COPY README.md ./

# Install dependencies (voice-input deps are darwin-only via platform marker)
RUN uv pip install --system --no-cache -e .

# Copy source
COPY . .

# Data directory — override with NORTH_HOME env var or mount a volume at /data
ENV NORTH_HOME=/data
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "orchestrator.app:app", \
     "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
