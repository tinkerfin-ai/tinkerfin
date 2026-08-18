#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
ENV_FILE="${SCRIPT_DIR}/.env"
SECRETS_DIR="${SCRIPT_DIR}/secrets"

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

command -v openssl >/dev/null 2>&1 || fail "未找到 openssl"
[ ! -e "${ENV_FILE}" ] || fail "部署配置已存在：${ENV_FILE}"
[ ! -e "${SECRETS_DIR}" ] || fail "Secrets 目录已存在：${SECRETS_DIR}"

umask 077
mkdir -p "${SECRETS_DIR}"
cp "${SCRIPT_DIR}/.env.example" "${ENV_FILE}"

mysql_root_password=$(openssl rand -hex 24)
mysql_password=$(openssl rand -hex 24)
redis_password=$(openssl rand -hex 24)
opensandbox_api_key=$(openssl rand -hex 32)

printf '%s\n' "${mysql_root_password}" > "${SECRETS_DIR}/mysql_root_password"
printf '%s\n' "${mysql_password}" > "${SECRETS_DIR}/mysql_password"
printf '%s\n' "${redis_password}" > "${SECRETS_DIR}/redis_password"
printf '%s\n' "${opensandbox_api_key}" > "${SECRETS_DIR}/opensandbox_api_key"
printf 'mysql+asyncmy://studio:%s@mysql:3306/tinkerfin?charset=utf8mb4\n' \
    "${mysql_password}" > "${SECRETS_DIR}/database_url"

chmod 700 "${SECRETS_DIR}"
chmod 600 "${SECRETS_DIR}"/* "${ENV_FILE}"
printf '已创建部署配置：%s\n' "${ENV_FILE}"
printf '已创建文件型 Secrets：%s\n' "${SECRETS_DIR}"
