#!/usr/bin/env sh

set -eu

PASSWORD=$(cat /run/secrets/mysql_password)
EXPECTED_TABLES='agent_models conversation_events conversation_interrupts conversation_messages conversation_runs conversation_threads store store_migrations tinkerfin_opensandbox_cleanup tinkerfin_opensandbox_owners tinkerfin_opensandbox_schema_versions tinkerfin_opensandbox_warm_slots tinkerfin_opensandbox_workers users'

mysql_command() {
    MYSQL_PWD="${PASSWORD}" mysql \
        --protocol=TCP \
        --host="${MYSQL_HOST}" \
        --port="${MYSQL_PORT}" \
        --user="${MYSQL_USER}" \
        "${MYSQL_DATABASE}" "$@"
}

attempt=0
until mysql_command --execute 'SELECT 1' >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "${attempt}" -lt 60 ] || {
        printf 'ERROR: MySQL 在等待窗口内未就绪\n' >&2
        exit 1
    }
    sleep 2
done

actual=$(mysql_command --batch --skip-column-names --execute 'SHOW TABLES' | sort | tr '\n' ' ' | sed 's/ $//')
if [ -z "${actual}" ]; then
    mysql_command < /opt/tinkerfin/schema.sql
    printf '已初始化 Studio 数据库结构\n'
elif [ "${actual}" = "${EXPECTED_TABLES}" ]; then
    printf 'Studio 数据库结构已存在\n'
else
    printf 'ERROR: 数据库不是空库且与当前完整结构不一致\n' >&2
    exit 1
fi
