#!/usr/bin/env bash
# 一条命令启动：建库 → 准备满仓/故障/退回演示数据 → Web 服务 + 后台任务
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8080}"
HOST="${HOST:-127.0.0.1}"

PY="$(command -v python3 || command -v python)"
echo "使用 Python: $($PY --version)"
echo "启动中… 数据目录 ./data （SQLite: locker.db，异常凭证: evidence/）"
exec "$PY" app.py --host "$HOST" --port "$PORT"
