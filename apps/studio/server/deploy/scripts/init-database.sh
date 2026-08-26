#!/usr/bin/env sh

set -eu

PASSWORD=$(cat /run/secrets/mysql_password)

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

first_table=$(mysql_command --batch --skip-column-names --execute 'SHOW TABLES' | sed -n '1p')
if [ -z "${first_table}" ]; then
    mysql_command < /opt/tinkerfin/schema.sql
    printf '已初始化 Studio 数据库结构\n'
else
    printf 'Studio 数据库已存在，跳过初始化\n'
fi
