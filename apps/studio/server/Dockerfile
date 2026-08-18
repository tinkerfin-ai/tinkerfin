# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.11.14
FROM python:${PYTHON_VERSION}-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS dependencies

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=120

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY dist/requirements.txt /tmp/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --disable-pip-version-check --require-hashes \
        --prefix=/opt/python -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

FROM python:${PYTHON_VERSION}-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d

ARG APP_VERSION=0.1.0

LABEL org.opencontainers.image.title="TinkerFin Studio" \
      org.opencontainers.image.version=${APP_VERSION}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN groupadd --gid 10001 tinkerfin \
    && useradd --uid 10001 --gid 10001 --create-home tinkerfin \
    && mkdir -p /app/logs \
    && chown -R 10001:10001 /app

COPY --from=dependencies /opt/python/ /usr/local/
COPY dist/*.whl /tmp/wheels/

RUN python -m pip install --disable-pip-version-check --no-deps \
        /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

USER 10001:10001

EXPOSE 8090

CMD ["python", "-m", "tinkerfin_studio", "--host", "0.0.0.0", "--port", "8090", "--no-reload"]
