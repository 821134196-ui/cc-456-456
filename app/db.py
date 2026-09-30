"""SQLite 存储层：建表、连接、事件流水。

所有包裹状态变化都写入 package_events，作为完整审计轨迹。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS buildings (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    neighbor_group INTEGER NOT NULL          -- 同组楼栋互为邻楼，可跨楼转移
);

CREATE TABLE IF NOT EXISTS lockers (
    id TEXT PRIMARY KEY,
    building_id TEXT NOT NULL REFERENCES buildings(id),
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cells (
    id TEXT PRIMARY KEY,
    locker_id TEXT NOT NULL REFERENCES lockers(id),
    code TEXT NOT NULL,                      -- 格口编号，如 A-01
    size TEXT NOT NULL CHECK(size IN ('S','M','L')),
    width_mm INTEGER NOT NULL,
    height_mm INTEGER NOT NULL,
    depth_mm INTEGER NOT NULL,
    door_status TEXT NOT NULL DEFAULT 'closed'
        CHECK(door_status IN ('closed','open','fault')),
    seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recipients (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,              -- 收件人代号，如 R-007
    name TEXT NOT NULL,                     -- 虚构姓名
    phone TEXT NOT NULL                     -- 虚构手机号
);

CREATE TABLE IF NOT EXISTS couriers (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,                  -- 快递公司
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'courier'    -- courier / property
);

CREATE TABLE IF NOT EXISTS packages (
    id TEXT PRIMARY KEY,
    waybill TEXT NOT NULL UNIQUE,           -- 虚构运单号
    company TEXT NOT NULL,
    recipient_id TEXT NOT NULL REFERENCES recipients(id),
    size TEXT NOT NULL CHECK(size IN ('S','M','L')),
    width_mm INTEGER NOT NULL,
    height_mm INTEGER NOT NULL,
    depth_mm INTEGER NOT NULL,
    status TEXT NOT NULL                    -- 见 STATUS_FLOW
        CHECK(status IN ('intransit','stored','on_shelf','in_transfer',
                         'ready_return','returned','sealed','picked_up')),
    current_cell_id TEXT REFERENCES cells(id),
    shelf_id TEXT,
    shelf_slot TEXT,
    locker_id TEXT,
    building_id TEXT,
    pickup_code TEXT,
    code_seq INTEGER NOT NULL DEFAULT 0,    -- 码版本：转移一次 +1，旧码立即失效
    stored_at TEXT,
    deadline_at TEXT,                       -- 存放时限
    extension_until TEXT,
    return_reason TEXT,
    reject_note TEXT,
    sealed_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS package_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,                    -- 操作人代号
    atype TEXT NOT NULL,                    -- 事件类型
    detail TEXT NOT NULL DEFAULT '{}',
    cell_id TEXT,
    old_code TEXT,
    new_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_pkg ON package_events(package_id, id);

CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL,
    from_type TEXT NOT NULL,                -- cell / shelf
    from_ref TEXT NOT NULL,
    to_type TEXT NOT NULL,                  -- cell / shelf / return
    to_ref TEXT NOT NULL,
    reason TEXT NOT NULL,
    old_code TEXT,
    new_code TEXT,
    courier_id TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shelf_slots (
    id TEXT PRIMARY KEY,                    -- SHF-A-03-02
    building_id TEXT NOT NULL REFERENCES buildings(id),
    rack_no TEXT NOT NULL,                  -- 架号 A
    level_no INTEGER NOT NULL,              -- 层号 3
    slot_no INTEGER NOT NULL,               -- 位号 02
    occupied_pkg TEXT
);

CREATE TABLE IF NOT EXISTS pickup_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL,
    proxy_name TEXT NOT NULL,
    proxy_phone TEXT NOT NULL,
    code TEXT NOT NULL,                     -- 授权码
    used INTEGER NOT NULL DEFAULT 0,
    created_ts TEXT NOT NULL,
    used_ts TEXT
);

CREATE TABLE IF NOT EXISTS force_open_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id TEXT NOT NULL,
    package_id TEXT,
    reason TEXT NOT NULL,
    applicant TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','rejected')),
    created_ts TEXT NOT NULL,
    decided_ts TEXT,
    approver TEXT,
    decision_note TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    phone TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'  -- pending/sent/failed
        CHECK(status IN ('pending','sent','failed')),
    retries INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    next_retry_at TEXT NOT NULL,
    last_error TEXT,
    created_ts TEXT NOT NULL,
    sent_ts TEXT,
    kind TEXT NOT NULL DEFAULT 'stored'     -- stored/transferred/overdue/ready_return/reject
);
CREATE INDEX IF NOT EXISTS idx_notif_retry
    ON notifications(status, next_retry_at);

CREATE TABLE IF NOT EXISTS handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- pickup / return / seal / force_open
    from_party TEXT NOT NULL,
    to_party TEXT NOT NULL,
    receiver_name TEXT,
    receiver_sign TEXT,
    evidence_path TEXT,
    note TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cell_locks (
    cell_id TEXT PRIMARY KEY,
    locked_by TEXT NOT NULL,                -- 快递员 id
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS door_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id TEXT NOT NULL,
    action TEXT NOT NULL,                   -- open / close
    result TEXT NOT NULL,                   -- success / failed
    detail TEXT,
    ts TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    """每个连接加 RLock；check_same_thread=False 以便工作线程共用。"""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.executescript(SCHEMA)

    # ---- 基础封装 ----
    def q(self, sql: str, args=()):
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def q1(self, sql: str, args=()):
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def x(self, sql: str, args=()):
        with self._lock:
            return self.conn.execute(sql, args)

    def tx(self, fn):
        """串行化事务：进程级锁 + BEGIN IMMEDIATE，杜绝格口竞争。"""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self.conn)
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ---- 事件 ----
    def event(self, conn, package_id, actor, atype, detail=None,
              cell_id=None, old_code=None, new_code=None):
        conn.execute(
            "INSERT INTO package_events(package_id,ts,actor,atype,detail,"
            "cell_id,old_code,new_code) VALUES(?,?,?,?,?,?,?,?)",
            (package_id, now(), actor, atype,
             json.dumps(detail or {}, ensure_ascii=False),
             cell_id, old_code, new_code),
        )

    def events(self, package_id):
        return self.q(
            "SELECT * FROM package_events WHERE package_id=? ORDER BY id", (package_id,))
