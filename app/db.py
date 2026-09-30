"""SQLite 数据层：商务楼快递柜系统的全部表结构与连接管理。"""
import os
import sqlite3
from datetime import datetime

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "parcel.db")
EVIDENCE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evidence")

SIZE_ORDER = {"S": 1, "M": 2, "L": 3}
SIZE_NAME = {"S": "小格口", "M": "中格口", "L": "大格口"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS company(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS building(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    neighbor_group TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courier_user(
    id TEXT PRIMARY KEY,
    company_id TEXT REFERENCES company(id),
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('courier','property','frontdesk','guest'))
);

CREATE TABLE IF NOT EXISTS locker(
    id TEXT PRIMARY KEY,
    building_id TEXT NOT NULL REFERENCES building(id),
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compartment(
    id TEXT PRIMARY KEY,
    locker_id TEXT NOT NULL REFERENCES locker(id),
    code TEXT NOT NULL,              -- 格口编号，如 A-01
    size TEXT NOT NULL CHECK(size IN ('S','M','L')),
    seq INTEGER NOT NULL,
    -- free 空闲 / allocating 已锁定待关门 / occupied 占用 / fault 故障维修
    status TEXT NOT NULL DEFAULT 'free',
    -- closed / open（门未关）/ fault（门锁故障）
    door_status TEXT NOT NULL DEFAULT 'closed',
    occupant_package_id TEXT REFERENCES package(id),
    alloc_token TEXT,
    fault_note TEXT
);

CREATE TABLE IF NOT EXISTS shelf(
    id TEXT PRIMARY KEY,
    building_id TEXT NOT NULL REFERENCES building(id),
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shelf_slot(
    id TEXT PRIMARY KEY,
    shelf_id TEXT NOT NULL REFERENCES shelf(id),
    rack_no TEXT NOT NULL,          -- 架号 A/B
    level_no INTEGER NOT NULL,      -- 层号 1..n
    pos_no INTEGER NOT NULL,        -- 层内位号
    status TEXT NOT NULL DEFAULT 'free',  -- free / occupied
    occupant_package_id TEXT REFERENCES package(id)
);

CREATE TABLE IF NOT EXISTS recipient(
    id TEXT PRIMARY KEY,
    alias TEXT NOT NULL,            -- 收件人代号（虚构，如 A-1207 周先生）
    phone TEXT NOT NULL             -- 虚构手机号
);

CREATE TABLE IF NOT EXISTS package(
    id TEXT PRIMARY KEY,
    waybill TEXT NOT NULL UNIQUE,   -- 运单号（虚构）
    company_id TEXT NOT NULL REFERENCES company(id),
    recipient_id TEXT NOT NULL REFERENCES recipient(id),
    building_id TEXT NOT NULL REFERENCES building(id),
    size TEXT NOT NULL CHECK(size IN ('S','M','L')),
    -- intake 待入柜 / stored 柜内 / on_shelf 货架 / overdue 超时
    -- picked 已取出 / returned 已退回 / sealed 异常封存 / misdirected 错投
    status TEXT NOT NULL DEFAULT 'intake',
    current_locker_id TEXT REFERENCES locker(id),
    current_compartment_id TEXT REFERENCES compartment(id),
    current_shelf_slot_id TEXT REFERENCES shelf_slot(id),
    delivered_by TEXT REFERENCES courier_user(id),
    ext_count INTEGER NOT NULL DEFAULT 0,
    stored_at TEXT,
    deadline_at TEXT,
    picked_at TEXT,
    picked_by TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pickup_code(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES package(id),
    code TEXT NOT NULL,
    -- active 有效 / void 已作废（转移或强开）/ used 已使用
    status TEXT NOT NULL DEFAULT 'active',
    issued_for TEXT NOT NULL,       -- 投放位置描述
    created_at TEXT NOT NULL,
    voided_at TEXT,
    void_reason TEXT
);

CREATE TABLE IF NOT EXISTS package_event(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT REFERENCES package(id),
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    actor TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS transfer_record(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES package(id),
    ts TEXT NOT NULL,
    actor TEXT,
    reason TEXT,                    -- full 满仓 / fault 故障 / size_mismatch 尺寸不符
    from_kind TEXT,                 -- locker / shelf / intake
    from_target TEXT,
    to_kind TEXT,                   -- locker / shelf
    to_target TEXT,
    old_code TEXT,
    new_code TEXT
);

CREATE TABLE IF NOT EXISTS notification(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES package(id),
    ts TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    phone TEXT NOT NULL,
    text TEXT NOT NULL,
    -- queued 待发 / sent 已送达 / failed 失败待重试
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    receipt_id TEXT
);

CREATE TABLE IF NOT EXISTS door_receipt(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    compartment_id TEXT NOT NULL REFERENCES compartment(id),
    ts TEXT NOT NULL,
    action TEXT NOT NULL,           -- open / close / force_open
    result TEXT NOT NULL,           -- success / fail
    detail TEXT
);

CREATE TABLE IF NOT EXISTS return_handoff(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES package(id),
    ts TEXT NOT NULL,
    status TEXT NOT NULL,           -- pending 待交接 / handed 已签收 / rejected 拒收
    reason TEXT,
    handed_to TEXT,
    handed_at TEXT,
    actor TEXT,
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS force_open_request(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT REFERENCES package(id),
    compartment_id TEXT NOT NULL REFERENCES compartment(id),
    ts TEXT NOT NULL,
    applicant TEXT NOT NULL,
    reason TEXT NOT NULL,
    -- pending 待审批 / approved 已批准 / rejected 已驳回 / executed 已执行
    status TEXT NOT NULL DEFAULT 'pending',
    approver TEXT,
    decided_at TEXT,
    executed_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS proxy_authorization(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES package(id),
    auth_code TEXT NOT NULL,
    authorized_name TEXT NOT NULL,
    ts TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'valid',  -- valid / used / void
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS evidence_file(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT REFERENCES package(id),
    kind TEXT NOT NULL,             -- fault 故障 / misdelivery 错投 / seal 封存 / reject 拒收
    ts TEXT NOT NULL,
    path TEXT NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_comp_locker ON compartment(locker_id);
CREATE INDEX IF NOT EXISTS idx_pkg_status ON package(status);
CREATE INDEX IF NOT EXISTS idx_code_code ON pickup_code(code, status);
CREATE INDEX IF NOT EXISTS idx_event_pkg ON package_event(package_id);
CREATE INDEX IF NOT EXISTS idx_slot_shelf ON shelf_slot(shelf_id);
"""


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def get_conn(db_path=None):
    path = db_path or os.environ.get("SMART_LOCKER_DB", DEFAULT_DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path=None):
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    return db_path or os.environ.get("SMART_LOCKER_DB", DEFAULT_DB_PATH)
