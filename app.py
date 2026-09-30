"""本地启动入口：python3 app.py
一条命令完成建库、写入满仓/故障/退回演示数据并启动 Web 服务与后台任务。
"""
from __future__ import annotations

import argparse
import os

from app.server import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="商务楼快递柜满仓转移与超时退回系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default="data/locker.db")
    ap.add_argument("--evidence", default="data/evidence")
    ap.add_argument("--keep-data", action="store_true",
                    help="不重置演示数据（默认每次启动重建满仓/故障/退回局面）")
    args = ap.parse_args()

    os.makedirs("data", exist_ok=True)
    serve(host=args.host, port=args.port, db_path=args.db,
          evidence_dir=args.evidence, reset=not args.keep_data)
