FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.13 /uv /bin/uv

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CANARY_SANDBOXED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependency layer first: cached unless the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --all-groups --no-emit-project -o /tmp/frozen.txt \
    && pip install --no-cache-dir --require-hashes -r /tmp/frozen.txt \
    && pip install --no-cache-dir hatchling==1.32.4 editables==0.6
COPY . /app
RUN pip install --no-cache-dir -e . --no-deps --no-build-isolation \
    && python -c "import canary; print('canary', canary.__file__)"

COPY scripts/container_entrypoint.sh /usr/local/bin/container_entrypoint.sh
ENTRYPOINT ["/usr/local/bin/container_entrypoint.sh"]
CMD ["pytest", "-q"]
