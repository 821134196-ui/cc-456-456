"""模拟适配器：智能门锁与短信网关，均落库产生回执。

门锁：open/close 返回 success/failed；故障门(door_status=fault)一律失败。
短信：默认成功，可通过 fail_next(phone, n) 注入失败以演示通知重试。
"""
from __future__ import annotations

import threading
from collections import defaultdict

from .db import now


class LockAdapter:
    def __init__(self, db):
        self.db = db

    def _command(self, conn, cell, action, force=False):
        was_fault = cell["door_status"] == "fault"
        # 物理状态约束：故障门任何操作都失败；关门只接受 open 态
        if was_fault and not force:
            result, detail = "failed", "door_fault：柜门故障，需物业维修"
        elif action == "open" and cell["door_status"] == "open":
            result, detail = "failed", "door_already_open：柜门处于打开状态"
        elif action == "close" and cell["door_status"] == "closed" and not force:
            result, detail = "failed", "door_already_closed"
        else:
            result, detail = "success", "ok"
        conn.execute(
            "INSERT INTO door_commands(cell_id,action,result,detail,ts) "
            "VALUES(?,?,?,?,?)",
            (cell["id"], action, result, detail, now()),
        )
        if result == "success":
            # 强开故障门：允许取出，但门保留 fault 状态等待维修，不再参与分配
            if not (force and was_fault):
                new_status = "open" if action == "open" else "closed"
                conn.execute("UPDATE cells SET door_status=? WHERE id=?",
                             (new_status, cell["id"]))
        return {"result": result, "detail": detail,
                "door_status": "fault" if (force and was_fault)
                else ("open" if action == "open" else "closed")}

    def open_door(self, cell, force=False):
        return self.db.tx(lambda c: self._command(c, cell, "open", force))

    def close_door(self, cell, force=False):
        return self.db.tx(lambda c: self._command(c, cell, "close", force))

    def mark_fault(self, cell_id):
        self.db.x("UPDATE cells SET door_status='fault' WHERE id=?", (cell_id,))

    def repair(self, cell_id):
        self.db.x("UPDATE cells SET door_status='closed' WHERE id=?", (cell_id,))


class SmsAdapter:
    """模拟短信网关；维护每号码待失败计数，用于通知重试演示。"""

    def __init__(self, db):
        self.db = db
        self._fail_left = defaultdict(int)
        self._lock = threading.Lock()

    def fail_next(self, phone, n=1):
        with self._lock:
            self._fail_left[phone] += n

    def send(self, notif_id):
        n = self.db.q1("SELECT * FROM notifications WHERE id=?", (notif_id,))
        if not n:
            return
        with self._lock:
            will_fail = self._fail_left[n["phone"]] > 0
            if will_fail:
                self._fail_left[n["phone"]] -= 1
        if will_fail:
            self.db.x(
                "UPDATE notifications SET status='failed',retries=retries+1,"
                "last_error=?,next_retry_at=? WHERE id=?",
                ("simulated_gateway_timeout", now(), notif_id),
            )
            return False
        self.db.x(
            "UPDATE notifications SET status='sent',sent_ts=?,last_error=NULL "
            "WHERE id=?",
            (now(), notif_id),
        )
        return True
