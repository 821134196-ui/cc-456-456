"""进程内定时任务：超时检测、退回入队、通知重试。

- overdue 扫描：超过 deadline_at 的 stored/on_shelf 包裹置为 overdue 并通知
- return 扫描：超过 deadline + 宽限期仍未取，生成退回交接单（pending）
- notify 重试：queued/failed 且未超过最大次数的短信重发
"""
import threading
import time
from datetime import datetime, timedelta

from . import db as dbmod
from .adapters import SmsAdapter
from .services import RETURN_GRACE_HOURS, STORAGE_LIMIT_HOURS

MAX_NOTIFY_ATTEMPTS = 5
TICK_SECONDS = 10


class Scheduler(threading.Thread):
    def __init__(self, tick=TICK_SECONDS, run_immediately=True):
        super().__init__(daemon=True, name="locker-scheduler")
        self.tick = tick
        self.run_immediately = run_immediately
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        if self.run_immediately:
            self.tick_once()
        while not self._stop.wait(self.tick):
            try:
                self.tick_once()
            except Exception as e:  # 单次失败不影响进程
                print(f"[scheduler] tick error: {e}")

    def tick_once(self):
        conn = dbmod.get_conn()
        try:
            stats = {
                "overdue": self._scan_overdue(conn),
                "return": self._scan_return(conn),
                "notify": self._retry_notifications(conn),
            }
            conn.commit()
            return stats
        finally:
            conn.close()

    def _scan_overdue(self, conn):
        now = datetime.now().replace(microsecond=0)
        rows = conn.execute(
            "SELECT * FROM package WHERE status IN ('stored','on_shelf') AND deadline_at <= ?",
            (now.isoformat(),),
        ).fetchall()
        sms = SmsAdapter(conn)
        for p in rows:
            conn.execute("UPDATE package SET status='overdue' WHERE id=?", (p["id"],))
            conn.execute(
                "INSERT INTO package_event(package_id, ts, type, actor, detail) VALUES (?,?,?,?,?)",
                (p["id"], dbmod.now_iso(), "overdue", "system",
                 f"超过存放时限({p['deadline_at']})仍未取，进入超时队列"),
            )
            r = conn.execute("SELECT phone FROM recipient WHERE id=?", (p["recipient_id"],)).fetchone()
            cur = conn.execute(
                "INSERT INTO notification(package_id, ts, phone, text, status, attempts) "
                "VALUES (?,?,?,?,'queued',0)",
                (p["id"], dbmod.now_iso(), r["phone"],
                 f"【商务楼快递柜】您的包裹{p['waybill']}已超时未取，已进入退回流程倒计时，"
                 f"{RETURN_GRACE_HOURS}小时内未取将退回快递公司。"),
            )
            notif = conn.execute("SELECT * FROM notification WHERE id=?", (cur.lastrowid,)).fetchone()
            sms.send(notif)
        return len(rows)

    def _scan_return(self, conn):
        cutoff = (datetime.now() - timedelta(hours=RETURN_GRACE_HOURS)).replace(microsecond=0)
        # deadline + 宽限 已过，且没有未完成退回单
        rows = conn.execute(
            "SELECT * FROM package p WHERE p.status='overdue' AND p.deadline_at <= ? "
            "AND NOT EXISTS (SELECT 1 FROM return_handoff rh WHERE rh.package_id=p.id "
            "AND rh.status IN ('pending','handed'))",
            (cutoff.isoformat(),),
        ).fetchall()
        sms = SmsAdapter(conn)
        for p in rows:
            conn.execute(
                "INSERT INTO return_handoff(package_id, ts, status, reason, actor) "
                "VALUES (?,?,'pending','超时无人取','system')",
                (p["id"], dbmod.now_iso()),
            )
            conn.execute(
                "INSERT INTO package_event(package_id, ts, type, actor, detail) VALUES (?,?,?,?,?)",
                (p["id"], dbmod.now_iso(), "return_queued", "system",
                 f"超时 {RETURN_GRACE_HOURS} 小时仍未取，进入退回交接队列"),
            )
            r = conn.execute("SELECT phone FROM recipient WHERE id=?", (p["recipient_id"],)).fetchone()
            cur = conn.execute(
                "INSERT INTO notification(package_id, ts, phone, text, status, attempts) "
                "VALUES (?,?,?,?,'queued',0)",
                (p["id"], dbmod.now_iso(), r["phone"],
                 f"【商务楼快递柜】您的包裹{p['waybill']}因超时无人取，已安排退回快递公司，如有疑问请联系前台。"),
            )
            sms.send(conn.execute("SELECT * FROM notification WHERE id=?", (cur.lastrowid,)).fetchone())
        return len(rows)

    def _retry_notifications(self, conn):
        sms = SmsAdapter(conn)
        rows = conn.execute(
            "SELECT * FROM notification WHERE status IN ('queued','failed') AND attempts < ? ORDER BY id",
            (MAX_NOTIFY_ATTEMPTS,),
        ).fetchall()
        n = 0
        for notif in rows:
            ok, _ = sms.send(notif)
            n += 1
        return n


def run_once():
    return Scheduler(run_immediately=True).tick_once()
