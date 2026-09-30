#!/usr/bin/env bash
# 一条本地命令运行商务楼快递柜满仓转移与超时退回系统。
#   ./run.sh          重置演示数据并在 :8000 启动
#   ./run.sh keep     保留现有数据启动
#   ./run.sh test     运行全部测试
#   ./run.sh tick     对运行中的系统手动触发一次超时/重试扫描
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

case "${1:-run}" in
  test)
    export SMART_LOCKER_DB="$(mktemp -d)/test.db"
    exec python3 -m unittest app.tests -v
    ;;
  tick)
    curl -sS -X POST "http://localhost:${PORT}/api/scheduler/tick" -H 'Content-Type: application/json' -d '{}'
    echo
    ;;
  run|reset)
    export SMART_LOCKER_RESET=1
    echo "启动商务楼快递柜系统（演示数据将被重置）…"
    exec python3 -m app.server "${PORT}"
    ;;
  keep)
    export SMART_LOCKER_RESET=0
    exec python3 -m app.server "${PORT}"
    ;;
  *)
    echo "用法: ./run.sh [run|keep|test|tick]  (端口可用 PORT=9000 ./run.sh)" >&2
    exit 1
    ;;
esac
