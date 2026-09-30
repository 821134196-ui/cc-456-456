"""进程内后台任务：
- 超时扫描：周期性把超过存放时限（含延期）的包裹移入 ready_return；
- 通知重试：发送 pending/failed 短信，失败按退避（10s/30s/90s）重试，
  超过 max_retries 置为 failed 终态（由人工处理）。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from .db import now

RETRY_BACKOFF = [10, 30, 90]


class Worker(threading.Thread):
    daemon = True

    def __init__(self, db, svc, interval=5):
        super().__init__(name="locker-worker")
        self.db = db
        self.svc = svc
        self.interval = interval
        self._stop = threading.Event()
        self.last_sweep = None
        self.last_retry = None

    def stop(self):
        self._stop.set()

    def run(self):
        # 启动即跑一轮，演示数据立即见效
        self.tick()
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:  # 后台任务不允许拖垮进程
                print(f"[worker] tick error: {e}")

    def tick(self):
        self.last_sweep = self.svc.sweep_overdue()
        self.last_retry = self.retry_notifications()

    def retry_notifications(self):
        sent, failed = [], []
        rows = self.db.q(
            "SELECT * FROM notifications WHERE status IN ('pending','failed')")
        for n in rows:
            if n["retries"] > n["max_retries"]:
                continue
            if n["status"] == "failed":
                due = _parse(n["next_retry_at"]) + timedelta(
                    seconds=RETRY_BACKOFF[min(n["retries"] - 1,
                                              len(RETRY_BACKOFF) - 1)])
                if datetime.now(timezone.utc) < due:
                    continue
            ok = self.svc.sms.send(n["id"])
            (sent if ok else failed).append(n["id"])
        return {"sent": sent, "failed": failed, "ts": now()}


def _parse(s):
    return datetime.fromisoformat(s)
