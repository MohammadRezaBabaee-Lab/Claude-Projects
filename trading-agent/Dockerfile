FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 1000 agent
WORKDIR /app

COPY pyproject.toml README.md ./
COPY trading_agent ./trading_agent
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir --upgrade "pip>=25" "setuptools>=83" && pip install --no-cache-dir ".[postgres]"

USER agent
RUN mkdir -p /app/data
# Default: paper mode, kill switch engaged. Override via environment, never by editing the image.
ENV TRADING_MODE=paper TRADING_ENABLED=false
CMD ["trading-agent", "run"]
