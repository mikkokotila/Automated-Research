FROM python:3.12-slim

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CANARY_SANDBOXED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -e . pytest \
    && python -c "import canary; print('canary', canary.__file__)"

COPY scripts/container_entrypoint.sh /usr/local/bin/container_entrypoint.sh
ENTRYPOINT ["/usr/local/bin/container_entrypoint.sh"]
CMD ["pytest", "-q"]
