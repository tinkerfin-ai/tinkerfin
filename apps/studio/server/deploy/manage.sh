#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
ENV_FILE="${SCRIPT_DIR}/.env"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yaml"

[ -f "${ENV_FILE}" ] || {
    printf 'ERROR: 缺少部署配置：%s\n' "${ENV_FILE}" >&2
    exit 1
}

exec docker compose \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    run --rm --no-deps studio \
    tinkerfin-studio-manage "$@"
