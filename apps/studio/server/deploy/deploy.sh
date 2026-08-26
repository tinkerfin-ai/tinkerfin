#!/usr/bin/env bash

set -Eeuo pipefail

readonly -a PROJECT_PATHS=(
    "packages/tinkerfin"
    "packages/tinkerfin-agui-adapter"
    "packages/tinkerfin-messaging"
    "packages/tinkerfin-sandbox"
    "apps/studio/server"
)
readonly MINIMUM_COMPOSE_VERSION="2.24.0"
DEPLOY_STARTED_AT=$SECONDS
CURRENT_STAGE="初始化"
CURRENT_COMMAND=""
EXTERNAL_MODE=0

fail() {
    printf '✗ %s\n' "$*" >&2
    return 1
}

handle_error() {
    local exit_code=$?
    trap - ERR
    printf '\n✗ 部署失败\n  阶段：%s\n' "$CURRENT_STAGE" >&2
    if [[ -n "$CURRENT_COMMAND" ]]; then
        printf '  命令：%s\n' "$CURRENT_COMMAND" >&2
    fi
    printf '  退出码：%d\n' "$exit_code" >&2
    exit "$exit_code"
}

handle_interrupt() {
    trap - INT TERM
    printf '\n⚠ 部署已取消\n' >&2
    exit 130
}

run_stage() {
    local title=$1
    local started_at=$SECONDS
    shift
    CURRENT_STAGE=$title
    printf -v CURRENT_COMMAND '%q ' "$@"
    CURRENT_COMMAND=${CURRENT_COMMAND% }
    printf '\n[%s]\n  $ %s\n' "$title" "$CURRENT_COMMAND"
    "$@"
    printf '✓ %s完成（%ds）\n' "$title" "$((SECONDS - started_at))"
}

usage() {
    cat <<EOF
Usage: $0 [--external] [--env-file FILE]

Options:
  --external       只启动 Studio，连接外部 MySQL、Redis 和 OpenSandbox
  --env-file FILE  指定部署环境文件
  -h, --help       显示帮助
EOF
}

version_at_least() {
    local actual=$1
    local required=$2
    awk -v actual="$actual" -v required="$required" 'BEGIN {
        split(actual, a, "."); split(required, r, ".")
        for (i = 1; i <= 3; i++) {
            if ((a[i] + 0) > (r[i] + 0)) exit 0
            if ((a[i] + 0) < (r[i] + 0)) exit 1
        }
        exit 0
    }'
}

compose() {
    local -a command=(
        docker compose
        --env-file "$ENV_FILE"
        -f "$COMPOSE_FILE"
    )
    if ((EXTERNAL_MODE == 0)); then
        command+=(--profile bundled)
    fi
    STUDIO_ENV_FILE=$ENV_FILE VERSION=${APP_VERSION:-0.1.0} "${command[@]}" "$@"
}

clean_build_artifacts() {
    local project
    for project in "${PROJECT_PATHS[@]}"; do
        rm -rf -- "$PROJECT_ROOT/$project/build"
        if [[ -d "$PROJECT_ROOT/$project/src" ]]; then
            find "$PROJECT_ROOT/$project/src" \
                -maxdepth 1 -type d -name '*.egg-info' \
                -exec rm -rf -- {} +
        fi
    done
}

build_release_artifacts() {
    local project
    clean_build_artifacts
    rm -rf -- "$PROJECT_ROOT/dist"
    mkdir -p "$PROJECT_ROOT/dist"
    uv lock --check
    uv export --quiet \
        --frozen \
        --package tinkerfin-studio \
        --no-dev \
        --no-emit-workspace \
        --no-header \
        --format requirements.txt \
        --output-file "$PROJECT_ROOT/dist/requirements.txt"
    for project in "${PROJECT_PATHS[@]}"; do
        uv build --quiet \
            --wheel \
            --out-dir "$PROJECT_ROOT/dist" \
            --no-create-gitignore \
            "$PROJECT_ROOT/$project"
    done
    clean_build_artifacts
}

prepare_external_database() {
    STUDIO_ENV_FILE=$ENV_FILE VERSION=${APP_VERSION:-0.1.0} docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        --profile tools \
        run --rm database-init
}

app_version_from_wheel() {
    local wheel
    wheel=$(find "$PROJECT_ROOT/dist" -maxdepth 1 \
        -name 'tinkerfin_studio-*.whl' -print -quit)
    [[ -n "$wheel" ]] || fail "未找到 Studio wheel"
    basename "$wheel" | sed -E 's/^tinkerfin_studio-//; s/-py3-none-any\.whl$//'
}

validate_environment() {
    local required
    local compose_version
    command -v docker >/dev/null 2>&1 || fail "未找到 Docker"
    command -v uv >/dev/null 2>&1 || fail "未找到 uv"
    docker info >/dev/null
    compose_version=$(docker compose version --short)
    version_at_least "$compose_version" "$MINIMUM_COMPOSE_VERSION" \
        || fail "Docker Compose 至少需要 ${MINIMUM_COMPOSE_VERSION}，当前为 ${compose_version}"
    for required in \
        "$PROJECT_ROOT/pyproject.toml" \
        "$PROJECT_ROOT/uv.lock" \
        "$PROJECT_ROOT/apps/studio/server/Dockerfile" \
        "$COMPOSE_FILE" \
        "$ENV_FILE" \
        "$SCRIPT_DIR/secrets/database_url" \
        "$SCRIPT_DIR/secrets/mysql_password" \
        "$SCRIPT_DIR/secrets/mysql_root_password" \
        "$SCRIPT_DIR/secrets/redis_password" \
        "$SCRIPT_DIR/secrets/opensandbox_api_key"; do
        [[ -f "$required" ]] || fail "缺少部署文件：$required"
    done
    compose config --quiet
}

deploy_wait_timeout() {
    local value
    value=$(awk -F '=' '$1 == "DEPLOY_WAIT_TIMEOUT" { print $2; exit }' "$ENV_FILE")
    printf '%s\n' "${value:-180}"
}

trap handle_error ERR
trap handle_interrupt INT TERM

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../../../.." && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yaml"
ENV_FILE="$SCRIPT_DIR/.env"

while (($#)); do
    case "$1" in
        --external)
            EXTERNAL_MODE=1
            shift
            ;;
        --env-file)
            [[ $# -ge 2 ]] || fail "--env-file 需要文件路径"
            ENV_FILE=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "未知参数：$1"
            ;;
    esac
done

readonly SCRIPT_DIR PROJECT_ROOT COMPOSE_FILE ENV_FILE EXTERNAL_MODE
cd "$PROJECT_ROOT"

run_stage "校验部署环境" validate_environment
run_stage "构建发布产物" build_release_artifacts
APP_VERSION=$(app_version_from_wheel)
readonly APP_VERSION
run_stage "构建生产镜像" compose build --pull studio
if ((EXTERNAL_MODE == 1)); then
    run_stage "准备外部数据库" prepare_external_database
fi
WAIT_TIMEOUT=$(deploy_wait_timeout)
readonly WAIT_TIMEOUT
run_stage "启动并等待后端" compose up -d --remove-orphans --wait \
    --wait-timeout "$WAIT_TIMEOUT"

CURRENT_STAGE="完成"
CURRENT_COMMAND=""
printf '\n🚀 Studio 后端部署完成，版本 %s（总耗时 %ds）\n' \
    "$APP_VERSION" "$((SECONDS - DEPLOY_STARTED_AT))"
printf '创建管理员：%s user create --username admin --display-name Admin\n' \
    "$SCRIPT_DIR/manage.sh"
