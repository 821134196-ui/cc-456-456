"""领域服务：入柜、格口匹配与竞争、满仓转移、人工货架、取件核验、
延期、超时、退回交接、异常封存、错投、强开审批、公司隔离。

关键规则：
- 关门回执成功才算入柜（stored）；门处于 open/fault 的格口不参与分配。
- 每次转移作废旧取件码（code_seq+1，pickup_code 更换）并发新短信通知。
- 格口操作走 cell_locks + BEGIN IMMEDIATE，两人争用仅一人成功。
- 快递员只能查看/操作本公司包裹；物业可见全部。
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

from .db import now
from .adapters import LockAdapter, SmsAdapter

SIZE_RANK = {"S": 1, "M": 2, "L": 3}
SIZE_NAME = {"S": "小", "M": "中", "L": "大"}

STORED_LIKE = ("stored", "on_shelf")


class DomainError(Exception):
    def __init__(self, code: str, message: str, extra=None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _fits(pkg, cell) -> bool:
    if SIZE_RANK[cell["size"]] < SIZE_RANK[pkg["size"]]:
        return False
    return (pkg["width_mm"] <= cell["width_mm"]
            and pkg["height_mm"] <= cell["height_mm"]
            and pkg["depth_mm"] <= cell["depth_mm"])


class Service:
    def __init__(self, db, evidence_dir, config=None):
        self.db = db
        self.lock = LockAdapter(db)
        self.sms = SmsAdapter(db)
        self.evidence_dir = evidence_dir
        os.makedirs(evidence_dir, exist_ok=True)
        self.cfg = {"store_hours": 24, "grace_hours": 12}
        if config:
            self.cfg.update(config)

    # ============ 工具 ============
    def _pkg(self, pkg_id):
        p = self.db.q1("SELECT * FROM packages WHERE id=?", (pkg_id,))
        if not p:
            raise DomainError("NOT_FOUND", "包裹不存在")
        return p

    def _cell(self, cell_id):
        c = self.db.q1("SELECT * FROM cells WHERE id=?", (cell_id,))
        if not c:
            raise DomainError("NOT_FOUND", "格口不存在")
        return c

    def require_company(self, actor, pkg):
        if actor["role"] == "courier" and pkg["company"] != actor["company"]:
            # 不泄露任何详情
            raise DomainError("FORBIDDEN", "无权查看其他公司的包裹")

    def _new_code(self, conn):
        for _ in range(50):
            code = f"{secrets.randbelow(10**6):06d}"
            dup = conn.execute(
                "SELECT 1 FROM packages WHERE pickup_code=? "
                "AND status IN ('stored','on_shelf','in_transfer','ready_return')",
                (code,)).fetchone()
            if not dup:
                return code
        raise DomainError("CODE_GEN_FAIL", "取件码生成失败")

    def _effective_deadline(self, pkg):
        ref = pkg["extension_until"] or pkg["deadline_at"]
        return _dt(ref) if ref else None

    def _notify(self, conn, pkg, kind, content):
        r = conn.execute("SELECT * FROM recipients WHERE id=?",
                         (pkg["recipient_id"],)).fetchone()
        conn.execute(
            "INSERT INTO notifications(package_id,phone,content,status,"
            "next_retry_at,created_ts,kind) VALUES(?,?,?, 'pending',?,?,?)",
            (pkg["id"], r["phone"], content, now(), now(), kind))

    def _save_evidence(self, pkg, kind, text, suffix="txt"):
        fname = f"{pkg['waybill']}_{kind}_{secrets.token_hex(3)}.{suffix}"
        path = os.path.join(self.evidence_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return os.path.abspath(path)

    # ============ 查询：柜机 / 格口 / 货架 ============
    def buildings(self):
        return self.db.q("SELECT * FROM buildings ORDER BY neighbor_group,id")

    def lockers_overview(self, building_id=None):
        sql = ("SELECT l.*, COUNT(c.id) n_total,"
               "SUM(CASE WHEN c.door_status='closed' THEN 1 ELSE 0 END) n_closed,"
               "SUM(CASE WHEN c.door_status='fault' THEN 1 ELSE 0 END) n_fault "
               "FROM lockers l LEFT JOIN cells c ON c.locker_id=l.id ")
        args = []
        if building_id:
            sql += "WHERE l.building_id=? "
            args.append(building_id)
        sql += "GROUP BY l.id ORDER BY l.id"
        out = []
        for l in self.db.q(sql, args):
            d = dict(l)
            occupied = self.db.q1(
                "SELECT COUNT(*) c FROM packages WHERE status IN "
                "('stored','ready_return','in_transfer') AND current_cell_id IS NOT NULL "
                "AND locker_id=?", (l["id"],))["c"]
            reserved = self.db.q1(
                "SELECT COUNT(*) c FROM cell_locks k JOIN cells c ON c.id=k.cell_id "
                "WHERE c.locker_id=?", (l["id"],))["c"]
            used = occupied + reserved
            d.update(occupied=occupied, reserved=reserved, used=used,
                     rate=round(used / l["n_total"], 4) if l["n_total"] else 0)
            out.append(d)
        return out

    def locker_cells(self, locker_id):
        """返回格口图数据，含物理尺寸、占用、门锁、锁持有者。"""
        rows = self.db.q(
            "SELECT c.*, p.id pkg_id, p.waybill, p.company, p.size pkg_size,"
            "p.pickup_code, p.status pkg_status, p.deadline_at, p.extension_until,"
            "k.locked_by, cu.name locked_by_name "
            "FROM cells c "
            "LEFT JOIN packages p ON p.current_cell_id=c.id AND p.status IN "
            "('stored','ready_return','in_transfer') "
            "LEFT JOIN cell_locks k ON k.cell_id=c.id "
            "LEFT JOIN couriers cu ON cu.id=k.locked_by "
            "WHERE c.locker_id=? ORDER BY c.seq", (locker_id,))
        return [dict(r) for r in rows]

    def shelf_grid(self, building_id):
        rows = self.db.q(
            "SELECT s.*, p.id pkg_id, p.waybill, p.company, p.pickup_code "
            "FROM shelf_slots s "
            "LEFT JOIN packages p ON p.shelf_id=s.id AND p.status='on_shelf' "
            "WHERE s.building_id=? ORDER BY s.rack_no,s.level_no,s.slot_no",
            (building_id,))
        return [dict(r) for r in rows]

    def _available_cell(self, conn, locker_id, pkg, exclude_locker_of=None):
        """最小可用格口：尺寸能容纳的最小一档，同档按编号；门必须关闭且无占用/锁。"""
        rows = conn.execute(
            "SELECT c.* FROM cells c JOIN lockers l ON l.id=c.locker_id "
            "WHERE l.enabled=1 AND c.door_status='closed' "
            "AND NOT EXISTS(SELECT 1 FROM packages p WHERE p.current_cell_id=c.id "
            "               AND p.status IN ('stored','in_transfer','ready_return')) "
            "AND NOT EXISTS(SELECT 1 FROM cell_locks k WHERE k.cell_id=c.id) "
            + ("AND c.locker_id=? " if locker_id else "")
            + ("AND c.locker_id<>? " if exclude_locker_of else "")
            + "ORDER BY CASE c.size WHEN 'S' THEN 1 WHEN 'M' THEN 2 ELSE 3 END, c.seq",
            tuple(x for x in (locker_id, exclude_locker_of) if x)).fetchall()
        for c in rows:
            if _fits(pkg, c):
                return c
        return None

    def suggest(self, actor, pkg_id, building_id):
        """满仓决策建议：本楼柜机最小格口 → 同楼人工货架 → 邻楼柜机。"""
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        building = self.db.q1("SELECT * FROM buildings WHERE id=?", (building_id,))
        plan = {"cell": None, "shelf_slots": [], "neighbor_cells": []}

        # 本楼任一柜机
        cell = self.db.tx(lambda c: self._find_in_building(c, building_id, pkg))
        plan["cell"] = dict(cell) if cell else None
        free_shelves = self.db.q(
            "SELECT * FROM shelf_slots WHERE building_id=? AND occupied_pkg IS NULL "
            "ORDER BY rack_no,level_no,slot_no LIMIT 20", (building_id,))
        plan["shelf_slots"] = [dict(s) for s in free_shelves[:6]]
        if not cell:
            for b in self.db.q(
                    "SELECT * FROM buildings WHERE neighbor_group=? AND id<>? "
                    "ORDER BY id", (building["neighbor_group"], building_id)):
                nc = self.db.tx(lambda c, b=b: self._find_in_building(c, b["id"], pkg))
                if nc:
                    d = dict(nc)
                    d["building_id"] = b["id"]
                    d["building_name"] = b["name"]
                    plan["neighbor_cells"].append(d)
        return plan

    def _find_in_building(self, conn, building_id, pkg):
        rows = conn.execute(
            "SELECT c.*, l.building_id FROM cells c JOIN lockers l ON l.id=c.locker_id "
            "WHERE l.building_id=? AND l.enabled=1 AND c.door_status='closed' "
            "AND NOT EXISTS(SELECT 1 FROM packages p WHERE p.current_cell_id=c.id "
            "               AND p.status IN ('stored','in_transfer','ready_return')) "
            "AND NOT EXISTS(SELECT 1 FROM cell_locks k WHERE k.cell_id=c.id) "
            "ORDER BY CASE c.size WHEN 'S' THEN 1 WHEN 'M' THEN 2 ELSE 3 END, c.seq",
            (building_id,)).fetchall()
        for c in rows:
            if _fits(pkg, c):
                return c
        return None

    # ============ 扫码 ============
    def scan(self, actor, waybill):
        p = self.db.q1("SELECT * FROM packages WHERE waybill=?", (waybill,))
        if not p:
            raise DomainError("UNKNOWN_WAYBILL", "运单未登记，请先录入包裹信息")
        if actor["role"] == "courier" and p["company"] != actor["company"]:
            # 重复/他司扫码都不泄露状态
            raise DomainError("FORBIDDEN", "该运单不属于您所在的公司")
        state = p["status"]
        if state == "stored":
            raise DomainError("DUP_STORED", "重复扫码：包裹已入柜，请勿重复投递",
                              {"status": state})
        if state == "on_shelf":
            raise DomainError("DUP_SHELF", "重复扫码：包裹已在人工货架",
                              {"status": state})
        if state in ("in_transfer",):
            raise DomainError("IN_TRANSFER", "包裹正在转移流程中")
        if state in ("returned", "picked_up", "sealed"):
            raise DomainError("DUP_CLOSED", f"重复扫码：包裹已结单（{state}）")
        lock = self.db.q1(
            "SELECT k.* FROM cell_locks k JOIN packages p ON 1=1 "
            "WHERE p.id=? AND k.cell_id=p.current_cell_id", (p["id"],))
        return {"package": self._public_pkg(p), "status": state,
                "locked": bool(lock)}

    def _public_pkg(self, p):
        d = dict(p)
        d.pop("pickup_code", None)  # 取件码不通过扫码接口回传
        return d

    def register_package(self, actor, waybill, recipient_code, size,
                         w, h, d):
        if size not in SIZE_RANK:
            raise DomainError("BAD_SIZE", "尺寸必须为 S/M/L")
        r = self.db.q1("SELECT * FROM recipients WHERE code=?", (recipient_code,))
        if not r:
            raise DomainError("NO_RECIPIENT", "收件人代号不存在")
        if self.db.q1("SELECT 1 FROM packages WHERE waybill=?", (waybill,)):
            raise DomainError("DUP_WAYBILL", "运单号已存在（重复扫码）")
        pid = f"PK-{secrets.token_hex(4)}"
        ts = now()
        self.db.x(
            "INSERT INTO packages(id,waybill,company,recipient_id,size,"
            "width_mm,height_mm,depth_mm,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?, 'intransit',?,?)",
            (pid, waybill, actor["company"], r["id"], size, w, h, d, ts, ts))
        self.db.event(self.db.conn, pid, actor["id"], "REGISTER",
                      {"waybill": waybill, "size": size})
        return self._pkg(pid)

    # ============ 入柜：争锁 → 开门 → 关门回执 ============
    def acquire_cell(self, actor, pkg_id, cell_id):
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        cell = self._cell(cell_id)
        if pkg["status"] != "intransit":
            raise DomainError("PKG_STATE", f"包裹当前状态 {pkg['status']}，不能入柜")
        if not _fits(pkg, cell):
            raise DomainError("SIZE_MISMATCH",
                              f"尺寸不符：{pkg['width_mm']}x{pkg['height_mm']}x"
                              f"{pkg['depth_mm']}mm 放不进 {SIZE_NAME[cell['size']]}"
                              f"格口 {cell['width_mm']}x{cell['height_mm']}x"
                              f"{cell['depth_mm']}mm")

        def work(conn):
            fresh = conn.execute("SELECT * FROM cells WHERE id=?",
                                 (cell_id,)).fetchone()
            occ = conn.execute(
                "SELECT 1 FROM packages WHERE current_cell_id=? AND status IN "
                "('stored','in_transfer','ready_return')",
                (cell_id,)).fetchone()
            if occ:
                raise DomainError("CELL_OCCUPIED", "格口已被占用")
            # 争用判定优先：他人持锁即竞争失败（无论门处于何种状态）
            holder = conn.execute("SELECT * FROM cell_locks WHERE cell_id=?",
                                  (cell_id,)).fetchone()
            if holder:
                if holder["locked_by"] == actor["id"]:
                    raise DomainError("ALREADY_LOCKED", "您已持有该格口锁")
                raise DomainError("CELL_LOCKED",
                                  "格口竞争失败：另一名快递员已获得该格口锁",
                                  {"locked_by": holder["locked_by"]})
            # 无主但门未关闭/故障 → 不可分配
            if fresh["door_status"] == "fault":
                raise DomainError("DOOR_FAULT", "柜门故障，该格口暂不可用")
            if fresh["door_status"] == "open":
                raise DomainError("DOOR_OPEN", "柜门尚未关闭，不能分配该格口")
            conn.execute("INSERT INTO cell_locks(cell_id,locked_by,ts) VALUES(?,?,?)",
                         (cell_id, actor["id"], now()))
            conn.execute("UPDATE packages SET current_cell_id=?,locker_id=?,"
                         "building_id=?,updated_at=? WHERE id=?",
                         (cell_id, fresh["locker_id"],
                          conn.execute("SELECT building_id FROM lockers WHERE id=?",
                                       (fresh["locker_id"],)).fetchone()[0],
                          now(), pkg_id))
            return True

        self.db.tx(work)
        # 开门（模拟器）；开门失败则释放预占
        opened = self.lock.open_door(self._cell(cell_id))
        if opened["result"] != "success":
            self._release(cell_id, actor["id"])
            if "fault" in opened["detail"]:
                raise DomainError("DOOR_FAULT", "门锁回执：柜门故障，无法打开")
            raise DomainError("DOOR_OPEN_FAIL", opened["detail"])
        self.db.event(self.db.conn, pkg_id, actor["id"], "DOOR_OPEN",
                      {"cell": cell_id}, cell_id=cell_id)
        return {"acquired": True, "cell_id": cell_id, "door": opened}

    def _release(self, cell_id, actor_id):
        self.db.x("DELETE FROM cell_locks WHERE cell_id=? AND locked_by=?",
                  (cell_id, actor_id))

    def close_receipt(self, actor, pkg_id):
        """快递员关门后上报；只有关门成功回执才正式入柜。"""
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        cell_id = pkg["current_cell_id"]
        if not cell_id:
            raise DomainError("NO_CELL", "包裹未分配格口")
        cell = self._cell(cell_id)
        receipt = self.lock.close_door(cell)
        if receipt["result"] != "success":
            # 关门失败（典型：关门时门体故障）→ 格口报修，快递员取回包裹改投
            self.lock.mark_fault(cell_id)
            self._release(cell_id, actor["id"])
            self.db.x("UPDATE packages SET current_cell_id=NULL,updated_at=? WHERE id=?",
                      (now(), pkg_id))
            self.db.event(self.db.conn, pkg_id, actor["id"], "CLOSE_FAULT_RETRIEVE",
                          {"cell": cell_id, "detail": receipt["detail"]},
                          cell_id=cell_id)
            raise DomainError("DOOR_FAULT_AT_CLOSE",
                              "关门回执失败：柜门故障，已请取回包裹并改投货架/邻楼",
                              {"cell_id": cell_id})

        def work(conn):
            ts = now()
            deadline = (datetime.now(timezone.utc)
                        + timedelta(hours=self.cfg["store_hours"])).isoformat(
                timespec="seconds")
            code = self._new_code(conn)
            conn.execute(
                "UPDATE packages SET status='stored', pickup_code=?, code_seq=code_seq+1,"
                "stored_at=?,deadline_at=?,extension_until=NULL,updated_at=? WHERE id=?",
                (code, ts, deadline, ts, pkg_id))
            conn.execute("DELETE FROM cell_locks WHERE cell_id=?", (cell_id,))
            self.db.event(conn, pkg_id, actor["id"], "STORED",
                          {"cell": cell_id, "deadline": deadline, "code_seq": pkg["code_seq"] + 1},
                          cell_id=cell_id, new_code=code)
            fresh = conn.execute("SELECT * FROM packages WHERE id=?",
                                 (pkg_id,)).fetchone()
            self._notify(conn, fresh, "stored",
                         f"【{fresh['company']}】您的快递 {fresh['waybill']} 已入柜"
                         f"（{SIZE_NAME[fresh['size']]}件），取件码 {code}，"
                         f"请于 {deadline[5:16].replace('T',' ')} 前取件。")
            return code

        code = self.db.tx(work)
        return {"stored": True, "cell_id": cell_id, "pickup_code": code,
                "receipt": receipt}

    def abort_stow(self, actor, pkg_id):
        """快递员放弃本次投递（开门后取回），关门并释放锁。"""
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        cell_id = pkg["current_cell_id"]
        if not cell_id:
            return {"aborted": True}
        cell = self._cell(cell_id)
        if cell["door_status"] == "open":
            self.lock.close_door(cell)
        self._release(cell_id, actor["id"])
        self.db.x("UPDATE packages SET current_cell_id=NULL,updated_at=? WHERE id=?",
                  (now(), pkg_id))
        self.db.event(self.db.conn, pkg_id, actor["id"], "STOW_ABORT",
                      {"cell": cell_id}, cell_id=cell_id)
        return {"aborted": True}

    # ============ 满仓转移 / 人工货架 ============
    def _approve_force(self, conn, cell_id, pkg_id):
        ap = conn.execute(
            "SELECT * FROM force_open_approvals WHERE cell_id=? AND package_id=? "
            "AND status='approved' ORDER BY id DESC LIMIT 1",
            (cell_id, pkg_id)).fetchone()
        return ap

    def transfer(self, actor, pkg_id, target, reason, force_approval_id=None):
        """转移：target=('cell', cell_id) 或 ('shelf', slot_id)。
        cell 目标开门后等待关门回执(confirm_transfer_close)；货架即时完成。
        每次转移作废旧码、生成新码与新通知。
        支持从 in_transfer 续接（强开取出后选择去向），此时源头已处理。"""
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        if pkg["status"] not in ("stored", "on_shelf", "ready_return",
                                 "in_transfer", "intransit"):
            raise DomainError("PKG_STATE", f"状态 {pkg['status']} 不可转移")
        # 入柜流程中已锁在打开的格口上（deliver 连续路径自己处理，不走这里）
        if pkg["status"] == "intransit" and pkg["current_cell_id"]:
            raise DomainError("PKG_STATE", "该包裹正处于开门投递中，请关门回执或放弃")
        src_cell = pkg["current_cell_id"]
        src_shelf = pkg["shelf_id"]
        src_is_fault = False
        if reason not in ("full", "fault", "misdelivery", "manual"):
            reason = "manual"

        # 源头开门：stored/ready_return 在柜内需要开门取出
        if src_cell and pkg["status"] in ("stored", "ready_return"):
            cell = self._cell(src_cell)
            src_is_fault = cell["door_status"] == "fault"
            if src_is_fault:
                ap = None
                if force_approval_id:
                    ap = self.db.q1(
                        "SELECT * FROM force_open_approvals WHERE id=? "
                        "AND status='approved' AND package_id=?",
                        (force_approval_id, pkg_id))
                if not ap:
                    raise DomainError("NEED_FORCE_OPEN",
                                      "源格口柜门故障，转移前必须取得物业强开审批")
                opened = self.lock.open_door(cell, force=True)
                self.db.event(self.db.conn, pkg_id, actor["id"], "FORCE_OPEN",
                              {"approval": force_approval_id}, cell_id=src_cell)
            else:
                opened = self.lock.open_door(cell)
            if opened["result"] != "success":
                raise DomainError("DOOR_OPEN_FAIL", opened["detail"])

        ttype, tid = target
        # 目标校验与开门
        target_cell = None
        target_slot = None
        if ttype == "cell":
            target_cell = self._cell(tid)
            if not _fits(pkg, target_cell):
                self._safe_close(src_cell)
                raise DomainError("SIZE_MISMATCH", "目标格口尺寸不符")

            def reserve(conn):
                fresh = conn.execute("SELECT * FROM cells WHERE id=?",
                                     (tid,)).fetchone()
                if fresh["door_status"] != "closed":
                    raise DomainError("DOOR_OPEN", "目标格口门未关闭，不能分配")
                if conn.execute("SELECT 1 FROM packages WHERE current_cell_id=? "
                                "AND status IN ('stored','in_transfer','ready_return')",
                                (tid,)).fetchone():
                    raise DomainError("CELL_OCCUPIED", "目标格口已占用")
                holder = conn.execute("SELECT * FROM cell_locks WHERE cell_id=?",
                                      (tid,)).fetchone()
                if holder and holder["locked_by"] != actor["id"]:
                    raise DomainError("CELL_LOCKED", "目标格口正被他人锁定")
                conn.execute("INSERT OR REPLACE INTO cell_locks(cell_id,locked_by,ts)"
                             " VALUES(?,?,?)", (tid, actor["id"], now()))

            self.db.tx(reserve)
            op = self.lock.open_door(target_cell)
            if op["result"] != "success":
                self._release(tid, actor["id"])
                self._safe_close(src_cell)
                raise DomainError("DOOR_OPEN_FAIL", "目标格口开门失败：" + op["detail"])
        else:
            target_slot = self.db.q1(
                "SELECT * FROM shelf_slots WHERE id=? AND occupied_pkg IS NULL",
                (tid,))
            if not target_slot:
                self._safe_close(src_cell)
                raise DomainError("SHELF_TAKEN", "货架位已被占用")

        # 包裹已取出 → 关源门（故障门强开后保留故障态，交物业维修）、清源头
        if src_cell and not src_is_fault:
            self.lock.close_door(self._cell(src_cell), force=True)
        old_code = pkg["pickup_code"]

        def midway(conn):
            conn.execute("UPDATE packages SET status='in_transfer',updated_at=? WHERE id=?",
                         (now(), pkg_id))
            if src_cell:
                if not src_is_fault:
                    conn.execute("UPDATE cells SET door_status='closed' WHERE id=?",
                                 (src_cell,))
                conn.execute("DELETE FROM cell_locks WHERE cell_id=?", (src_cell,))
                conn.execute("INSERT INTO handovers(package_id,kind,from_party,"
                             "to_party,note,ts) VALUES(?,?,?,?,?,?)",
                             (pkg_id, "transfer_out", "cell:" + src_cell,
                              actor["id"], reason, now()))
            if src_shelf:
                conn.execute("UPDATE shelf_slots SET occupied_pkg=NULL WHERE id=?",
                             (src_shelf,))
            if ttype == "cell":
                conn.execute("UPDATE packages SET current_cell_id=?,shelf_id=NULL,"
                             "shelf_slot=NULL,locker_id=?,building_id=? WHERE id=?",
                             (tid, target_cell["locker_id"],
                              conn.execute("SELECT building_id FROM lockers WHERE id=?",
                                           (target_cell["locker_id"],)).fetchone()[0],
                              pkg_id))
            else:
                conn.execute("UPDATE packages SET current_cell_id=NULL,shelf_id=?,"
                             "shelf_slot=?,locker_id=NULL,building_id=? WHERE id=?",
                             (tid, target_slot["id"], target_slot["building_id"],
                              pkg_id))
            conn.execute(
                "INSERT INTO transfers(package_id,from_type,from_ref,to_type,to_ref,"
                "reason,old_code,courier_id,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                (pkg_id, "cell" if src_cell else "shelf",
                 src_cell or src_shelf or "-", ttype, tid, reason,
                 old_code, actor["id"], now()))
            self.db.event(conn, pkg_id, actor["id"], "TRANSFER",
                          {"from": src_cell or src_shelf, "to": tid, "reason": reason},
                          cell_id=tid if ttype == "cell" else None,
                          old_code=old_code)

        self.db.tx(midway)

        if ttype == "shelf":
            return self._finalize_shelf(actor, pkg_id, target_slot, old_code, reason)
        return {"await_close": True, "target_cell": dict(target_cell),
                "old_code": old_code, "reason": reason}

    def _safe_close(self, cell_id):
        if not cell_id:
            return
        c = self._cell(cell_id)
        if c["door_status"] == "open":
            self.lock.close_door(c, force=True)

    def confirm_transfer_close(self, actor, pkg_id):
        """转移到格口后的关门回执：成功才换码、通知、完成转移。"""
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        if pkg["status"] != "in_transfer" or not pkg["current_cell_id"]:
            raise DomainError("PKG_STATE", "包裹不在待关门的转移状态")
        cell = self._cell(pkg["current_cell_id"])
        receipt = self.lock.close_door(cell)
        if receipt["result"] != "success":
            self.lock.mark_fault(cell["id"])
            self._release(cell["id"], actor["id"])
            raise DomainError("DOOR_FAULT_AT_CLOSE",
                              "关门回执失败：目标格口故障，请取回包裹重新选择去向")
        old_code = self.db.q1(
            "SELECT old_code FROM transfers WHERE package_id=? ORDER BY id DESC LIMIT 1",
            (pkg_id,))["old_code"]
        return self._finalize_cell(actor, pkg_id, cell, old_code)

    def _finalize_cell(self, actor, pkg_id, cell, old_code):
        def work(conn):
            code = self._new_code(conn)
            deadline = (datetime.now(timezone.utc)
                        + timedelta(hours=self.cfg["store_hours"])).isoformat(
                timespec="seconds")
            conn.execute(
                "UPDATE packages SET status='stored',pickup_code=?,code_seq=code_seq+1,"
                "stored_at=?,deadline_at=?,extension_until=NULL,updated_at=? WHERE id=?",
                (code, now(), deadline, now(), pkg_id))
            conn.execute("DELETE FROM cell_locks WHERE cell_id=?", (cell["id"],))
            conn.execute("UPDATE transfers SET new_code=? WHERE package_id=? "
                         "AND new_code IS NULL ORDER BY id DESC LIMIT 1",
                         (code, pkg_id))
            self.db.event(conn, pkg_id, actor["id"], "TRANSFER_STORED",
                          {"cell": cell["id"]}, cell_id=cell["id"],
                          old_code=old_code, new_code=code)
            fresh = conn.execute("SELECT * FROM packages WHERE id=?",
                                 (pkg_id,)).fetchone()
            self._notify(conn, fresh, "transferred",
                         f"【{fresh['company']}】您的快递 {fresh['waybill']} 已转移至"
                         f"新格口，旧取件码已作废。新取件码 {code}，"
                         f"请于 {deadline[5:16].replace('T',' ')} 前取件。")
            return code

        code = self.db.tx(work)
        return {"stored": True, "cell_id": cell["id"], "pickup_code": code,
                "old_code_invalid": old_code}

    def _finalize_shelf(self, actor, pkg_id, slot, old_code, reason):
        def work(conn):
            code = self._new_code(conn)
            conn.execute("UPDATE shelf_slots SET occupied_pkg=? WHERE id=?",
                         (pkg_id, slot["id"]))
            deadline = (datetime.now(timezone.utc)
                        + timedelta(hours=self.cfg["store_hours"])).isoformat(
                timespec="seconds")
            conn.execute(
                "UPDATE packages SET status='on_shelf',pickup_code=?,code_seq=code_seq+1,"
                "stored_at=COALESCE(stored_at,?),deadline_at=?,extension_until=NULL,"
                "updated_at=? WHERE id=?",
                (code, now(), deadline, now(), pkg_id))
            conn.execute("UPDATE transfers SET new_code=? WHERE package_id=? "
                         "AND new_code IS NULL ORDER BY id DESC LIMIT 1",
                         (code, pkg_id))
            self.db.event(conn, pkg_id, actor["id"], "TRANSFER_SHELF",
                          {"slot": slot["id"], "rack": slot["rack_no"],
                           "level": slot["level_no"], "pos": slot["slot_no"]},
                          old_code=old_code, new_code=code)
            fresh = conn.execute("SELECT * FROM packages WHERE id=?",
                                 (pkg_id,)).fetchone()
            self._notify(conn, fresh, "transferred",
                         f"【{fresh['company']}】您的快递 {fresh['waybill']} 因柜机满仓"
                         f"已转入{slot['rack_no']}架{slot['level_no']}层{slot['slot_no']:02d}位"
                         f"人工货架，旧取件码已作废。货架取件码 {code}，请向前台出示。")
            return code

        code = self.db.tx(work)
        return {"on_shelf": True, "slot": dict(slot), "pickup_code": code,
                "old_code_invalid": old_code}

    def transfer_chain(self, pkg_id):
        return [dict(r) for r in self.db.q(
            "SELECT * FROM transfers WHERE package_id=? ORDER BY id", (pkg_id,))]

    # ============ 取件核验 ============
    def create_authorization(self, actor, pkg_id, proxy_name, proxy_phone):
        """收件人凭有效取件码为前台代取建立授权（登记代取人姓名/手机号）。"""
        pkg = self._pkg(pkg_id)
        if pkg["status"] not in STORED_LIKE:
            raise DomainError("PKG_STATE", "包裹当前不可代取授权")
        auth_code = f"A{secrets.randbelow(10**6):06d}"
        ts = now()
        self.db.x(
            "INSERT INTO pickup_authorizations(package_id,proxy_name,proxy_phone,"
            "code,created_ts) VALUES(?,?,?,?,?)",
            (pkg_id, proxy_name, proxy_phone, auth_code, ts))
        self.db.event(self.db.conn, pkg_id, actor["id"], "AUTH_CREATE",
                      {"proxy": proxy_name, "phone": proxy_phone})
        return {"auth_code": auth_code}

    def pickup(self, actor, code, auth_code=None, proxy_name=None,
               proxy_phone=None, receiver_sign=""):
        pkg = self.db.q1(
            "SELECT * FROM packages WHERE pickup_code=? AND status IN "
            "('stored','on_shelf','in_transfer','ready_return')", (code,))
        if not pkg:
            raise DomainError("BAD_CODE", "取件码错误或已失效（转移后旧码立即作废）")
        # 码版本核对：调用方给出的码即当前码；旧码自然查不到 → BAD_CODE
        if pkg["status"] == "in_transfer":
            raise DomainError("IN_TRANSFER", "包裹转移中，请使用最新通知里的新码")
        deadline = self._effective_deadline(pkg)
        if deadline and datetime.now(timezone.utc) > deadline:
            raise DomainError("OVERDUE", "已超过存放时限，包裹进入退回流程，请联系前台")

        auth = None
        if auth_code:
            auth = self.db.q1(
                "SELECT * FROM pickup_authorizations WHERE code=? AND package_id=? "
                "AND used=0", (auth_code, pkg["id"]))
            if not auth:
                raise DomainError("BAD_AUTH", "授权码无效或已使用")
            if proxy_name != auth["proxy_name"] or proxy_phone != auth["proxy_phone"]:
                raise DomainError("AUTH_MISMATCH",
                                  "前台代取核对失败：代取人姓名/手机号与授权登记不一致")

        if pkg["status"] == "on_shelf":
            return self._pickup_shelf(actor, pkg, auth, receiver_sign)
        return self._pickup_cell(actor, pkg, auth, receiver_sign)

    def _pickup_cell(self, actor, pkg, auth, sign):
        cell = self._cell(pkg["current_cell_id"])
        op = self.lock.open_door(cell)
        if op["result"] != "success":
            raise DomainError("DOOR_FAULT", "柜门故障，请联系物业走强开审批")
        # 取出后关门回执
        self.lock.close_door(self._cell(cell["id"]))

        def work(conn):
            conn.execute("UPDATE packages SET status='picked_up',current_cell_id=NULL,"
                         "updated_at=? WHERE id=?", (now(), pkg["id"]))
            if auth:
                conn.execute("UPDATE pickup_authorizations SET used=1,used_ts=? WHERE id=?",
                             (now(), auth["id"]))
            who = auth["proxy_name"] if auth else "收件人本人"
            conn.execute("INSERT INTO handovers(package_id,kind,from_party,to_party,"
                         "receiver_name,receiver_sign,ts) VALUES(?,?,?,?,?,?,?)",
                         (pkg["id"], "pickup", "locker:" + cell["id"],
                          auth["proxy_phone"] if auth else "recipient", who, sign, now()))
            self.db.event(conn, pkg["id"], actor["id"], "PICKED_UP",
                          {"by": who, "auth": bool(auth)}, cell_id=cell["id"],
                          old_code=pkg["pickup_code"])

        self.db.tx(work)
        return {"picked_up": True, "cell_id": cell["id"]}

    def _pickup_shelf(self, actor, pkg, auth, receiver_sign):
        slot = self.db.q1("SELECT * FROM shelf_slots WHERE id=?", (pkg["shelf_id"],))

        def work(conn):
            conn.execute("UPDATE shelf_slots SET occupied_pkg=NULL WHERE id=?",
                         (slot["id"],))
            conn.execute("UPDATE packages SET status='picked_up',shelf_id=NULL,"
                         "shelf_slot=NULL,updated_at=? WHERE id=?", (now(), pkg["id"]))
            if auth:
                conn.execute("UPDATE pickup_authorizations SET used=1,used_ts=? WHERE id=?",
                             (now(), auth["id"]))
            who = auth["proxy_name"] if auth else "收件人本人"
            sign_value = receiver_sign or who
            conn.execute("INSERT INTO handovers(package_id,kind,from_party,to_party,"
                         "receiver_name,receiver_sign,ts) VALUES(?,?,?,?,?,?,?)",
                         (pkg["id"], "pickup", "shelf:" + slot["id"],
                          auth["proxy_phone"] if auth else "recipient", who,
                          sign_value, now()))
            self.db.event(conn, pkg["id"], actor["id"], "PICKED_UP_SHELF",
                          {"by": who, "slot": slot["id"], "auth": bool(auth)},
                          old_code=pkg["pickup_code"])

        self.db.tx(work)
        return {"picked_up": True, "slot": slot["id"]}

    # ============ 延期 ============
    def extend(self, actor, pkg_id, hours):
        pkg = self._pkg(pkg_id)
        if actor["role"] == "courier":
            self.require_company(actor, pkg)
        if pkg["status"] not in STORED_LIKE:
            raise DomainError("PKG_STATE", "仅在存包裹可申请延期")
        base = self._effective_deadline(pkg)
        if datetime.now(timezone.utc) > base:
            raise DomainError("ALREADY_OVERDUE", "已超时，不能延期，请走退回流程")
        hours = max(1, min(int(hours), 72))
        new_until = (base + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.db.x("UPDATE packages SET extension_until=?,updated_at=? WHERE id=?",
                  (new_until, now(), pkg_id))
        self.db.event(self.db.conn, pkg_id, actor["id"], "EXTEND",
                      {"hours": hours, "until": new_until})
        return {"extension_until": new_until}

    # ============ 超时队列（worker 调用，也可手动触发） ============
    def sweep_overdue(self):
        moved = []

        def work(conn):
            rows = conn.execute(
                "SELECT * FROM packages WHERE status IN ('stored','on_shelf')").fetchall()
            for p in rows:
                ref = p["extension_until"] or p["deadline_at"]
                if ref and datetime.now(timezone.utc) > _dt(ref):
                    conn.execute("UPDATE packages SET status='ready_return',"
                                 "return_reason='timeout',updated_at=? WHERE id=?",
                                 (now(), p["id"]))
                    self.db.event(conn, p["id"], "system", "OVERDUE",
                                  {"deadline": ref})
                    self._notify(conn, p, "overdue",
                                 f"【{p['company']}】您的快递 {p['waybill']} 已超过存放"
                                 f"时限，请立即联系前台；逾期未取将退回寄件方。")
                    moved.append(p["id"])

        self.db.tx(work)
        return moved

    def overdue_queue(self):
        rows = self.db.q(
            "SELECT p.*, r.name rname, r.phone rphone, b.name bname "
            "FROM packages p JOIN recipients r ON r.id=p.recipient_id "
            "LEFT JOIN buildings b ON b.id=p.building_id "
            "WHERE p.status IN ('stored','on_shelf') ORDER BY "
            "COALESCE(p.extension_until,p.deadline_at)")
        out = []
        nowdt = datetime.now(timezone.utc)
        for r in rows:
            d = dict(r)
            dl = self._effective_deadline(r)
            d["effective_deadline"] = dl.isoformat()
            d["remain_hours"] = round((dl - nowdt).total_seconds() / 3600, 1)
            out.append(d)
        return out

    # ============ 退回交接 / 拒收 ============
    def return_queue(self):
        return [dict(r) for r in self.db.q(
            "SELECT p.*, r.name rname, r.phone rphone, b.name bname "
            "FROM packages p JOIN recipients r ON r.id=p.recipient_id "
            "LEFT JOIN buildings b ON b.id=p.building_id "
            "WHERE p.status='ready_return' ORDER BY p.updated_at")]

    def confirm_return(self, actor, pkg_id, receiver_name, sign, note=""):
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        if pkg["status"] != "ready_return":
            raise DomainError("PKG_STATE", "包裹不在待退回队列")
        cell_id = pkg["current_cell_id"]
        if cell_id:
            cell = self._cell(cell_id)
            if cell["door_status"] == "fault":
                raise DomainError("NEED_FORCE_OPEN", "格口故障，须先强开取件再交接")
            self.lock.open_door(cell)
            self.lock.close_door(self._cell(cell_id))

        def work(conn):
            conn.execute("UPDATE packages SET status='returned',current_cell_id=NULL,"
                         "updated_at=? WHERE id=?", (now(), pkg_id))
            conn.execute("INSERT INTO handovers(package_id,kind,from_party,to_party,"
                         "receiver_name,receiver_sign,note,ts) VALUES(?,?,?,?,?,?,?,?)",
                         (pkg_id, "return", "property", pkg["company"],
                          receiver_name, sign or receiver_name, note, now()))
            self.db.event(conn, pkg_id, actor["id"], "RETURNED",
                          {"receiver": receiver_name, "reason": pkg["return_reason"]},
                          cell_id=cell_id)

        self.db.tx(work)
        return {"returned": True}

    def reject_return(self, actor, pkg_id, note):
        """退回拒收：交接失败，留存凭证并通知收件人，包裹留在待退回队列。"""
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        if pkg["status"] != "ready_return":
            raise DomainError("PKG_STATE", "包裹不在待退回队列")
        path = self._save_evidence(
            pkg, "return_reject",
            f"运单 {pkg['waybill']}\n公司 {pkg['company']}\n拒收时间 {now()}\n"
            f"拒收人 {actor['name']}({actor['company']})\n说明 {note}\n")

        def work(conn):
            conn.execute("UPDATE packages SET reject_note=?,updated_at=? WHERE id=?",
                         (note, now(), pkg_id))
            conn.execute("INSERT INTO handovers(package_id,kind,from_party,to_party,"
                         "receiver_name,evidence_path,note,ts) VALUES(?,?,?,?,?,?,?,?)",
                         (pkg_id, "return_reject", pkg["company"], "property",
                          actor["name"], path, note, now()))
            self.db.event(conn, pkg_id, actor["id"], "RETURN_REJECTED",
                          {"note": note, "evidence": path})
            self._notify(conn, pkg, "reject",
                         f"【{pkg['company']}】您的快递 {pkg['waybill']} 退回交接被拒收"
                         f"（{note}），物业将重新协调处理，请保持电话畅通。")

        self.db.tx(work)
        return {"rejected": True, "evidence": path}

    # ============ 异常封存 / 错投 / 故障 / 强开 ============
    def mark_misdelivery(self, actor, pkg_id, correct_building_id=None, note=""):
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        self.db.event(self.db.conn, pkg_id, actor["id"], "MISDELIVERY",
                      {"correct_building": correct_building_id, "note": note})
        if correct_building_id:
            # 转入正确楼栋：先找柜机，没有则货架
            target = self.db.tx(
                lambda c: self._find_in_building(c, correct_building_id, pkg))
            if target:
                r = self.transfer(actor, pkg_id, ("cell", target["id"]),
                                  "misdelivery")
                if r.get("await_close"):
                    r2 = self.confirm_transfer_close(actor, pkg_id)
                    return {"misdelivery": "transferred_cell", **r2}
                return {"misdelivery": "transferred_shelf", **r}
            slot = self.db.q1(
                "SELECT * FROM shelf_slots WHERE building_id=? AND occupied_pkg IS NULL "
                "ORDER BY rack_no,level_no,slot_no LIMIT 1", (correct_building_id,))
            if slot:
                r = self.transfer(actor, pkg_id, ("shelf", slot["id"]), "misdelivery")
                return {"misdelivery": "transferred_shelf", **r}
            raise DomainError("NO_CAPACITY", "正确楼栋也无容量，请先异常封存")
        return {"misdelivery": "flagged"}

    def seal_package(self, actor, pkg_id, reason, sign, note=""):
        pkg = self._pkg(pkg_id)
        if actor["role"] == "courier":
            self.require_company(actor, pkg)
        if pkg["status"] not in ("on_shelf", "ready_return", "in_transfer", "intransit"):
            raise DomainError("PKG_STATE",
                              "柜内包裹须先经强开取出后才能封存")
        path = self._save_evidence(
            pkg, "seal",
            f"运单 {pkg['waybill']}\n封存时间 {now()}\n经办人 {actor['name']}\n"
            f"原因 {reason}\n备注 {note}\n签名 {sign}\n")

        def work(conn):
            if pkg["shelf_id"]:
                conn.execute("UPDATE shelf_slots SET occupied_pkg=NULL WHERE id=?",
                             (pkg["shelf_id"],))
            conn.execute("UPDATE packages SET status='sealed',sealed_reason=?,"
                         "current_cell_id=NULL,shelf_id=NULL,shelf_slot=NULL,"
                         "updated_at=? WHERE id=?", (reason, now(), pkg_id))
            conn.execute("INSERT INTO handovers(package_id,kind,from_party,to_party,"
                         "receiver_name,receiver_sign,evidence_path,note,ts)"
                         " VALUES(?,?,?,?,?,?,?,?,?)",
                         (pkg_id, "seal", actor["id"], "property_vault",
                          actor["name"], sign, path, reason + " " + note, now()))
            self.db.event(conn, pkg_id, actor["id"], "SEALED",
                          {"reason": reason, "evidence": path})

        self.db.tx(work)
        return {"sealed": True, "evidence": path}

    def report_fault(self, actor, cell_id, note=""):
        cell = self._cell(cell_id)
        self.lock.mark_fault(cell_id)
        pkg = self.db.q1("SELECT * FROM packages WHERE current_cell_id=? AND status IN "
                         "('stored','ready_return','in_transfer')", (cell_id,))
        self.db.event(self.db.conn, pkg["id"] if pkg else "-",
                      actor["id"], "DOOR_FAULT_REPORT",
                      {"cell": cell_id, "note": note,
                       "pkg_inside": pkg["id"] if pkg else None},
                      cell_id=cell_id)
        return {"fault": True, "pkg_inside": pkg["id"] if pkg else None}

    def repair_cell(self, actor, cell_id):
        if actor["role"] != "property":
            raise DomainError("FORBIDDEN", "仅物业可维修结案")
        self.lock.repair(cell_id)
        self.db.event(self.db.conn, "-", actor["id"], "REPAIRED",
                      {"cell": cell_id}, cell_id=cell_id)
        return {"repaired": True}

    # ---- 强开审批 ----
    def apply_force_open(self, actor, cell_id, reason):
        cell = self._cell(cell_id)
        pkg = self.db.q1("SELECT * FROM packages WHERE current_cell_id=? AND status IN "
                         "('stored','ready_return','in_transfer')", (cell_id,))
        if actor["role"] == "courier" and pkg and pkg["company"] != actor["company"]:
            raise DomainError("FORBIDDEN", "无权为他司包裹申请强开")
        ts = now()
        cur = self.db.x(
            "INSERT INTO force_open_approvals(cell_id,package_id,reason,applicant,"
            "created_ts) VALUES(?,?,?,?,?)",
            (cell_id, pkg["id"] if pkg else None, reason, actor["id"], ts))
        return {"approval_id": cur.lastrowid, "pkg": pkg["id"] if pkg else None}

    def decide_force_open(self, actor, approval_id, approve, note=""):
        if actor["role"] != "property":
            raise DomainError("FORBIDDEN", "强制开柜必须经物业审批")
        ap = self.db.q1("SELECT * FROM force_open_approvals WHERE id=?", (approval_id,))
        if not ap:
            raise DomainError("NOT_FOUND", "审批单不存在")
        if ap["status"] != "pending":
            raise DomainError("ALREADY_DECIDED", "该审批已处理")
        self.db.x("UPDATE force_open_approvals SET status=?,decided_ts=?,approver=?,"
                  "decision_note=? WHERE id=?",
                  ("approved" if approve else "rejected", now(), actor["id"], note,
                   approval_id))
        self.db.event(self.db.conn, ap["package_id"] or "-", actor["id"],
                      "FORCE_APPROVAL",
                      {"approval": approval_id, "result": approve, "note": note},
                      cell_id=ap["cell_id"])
        return {"decided": "approved" if approve else "rejected"}

    def approvals(self, status=None):
        sql = ("SELECT a.*, c.code cell_code, l.name locker_name, p.waybill "
               "FROM force_open_approvals a JOIN cells c ON c.id=a.cell_id "
               "JOIN lockers l ON l.id=c.locker_id "
               "LEFT JOIN packages p ON p.id=a.package_id ")
        args = []
        if status:
            sql += "WHERE a.status=? "
            args.append(status)
        sql += "ORDER BY a.id DESC"
        return [dict(r) for r in self.db.q(sql, args)]

    def force_retrieve(self, actor, pkg_id, approval_id):
        """凭审批强开取出包裹：门打开→取出→门留故障态，包裹进入 in_transfer，
        随后必须选择转移去向或封存。"""
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        ap = self.db.q1(
            "SELECT * FROM force_open_approvals WHERE id=? AND status='approved'",
            (approval_id,))
        if not ap or ap["package_id"] != pkg_id:
            raise DomainError("NO_APPROVAL", "没有匹配的已批准强开审批")
        cell = self._cell(pkg["current_cell_id"])
        op = self.lock.open_door(cell, force=True)
        if op["result"] != "success":
            raise DomainError("FORCE_OPEN_FAIL", op["detail"])

        def work(conn):
            # 待退回件强开取出后仍走退回交接；柜内正常件进入转移中等待去向
            new_status = ("ready_return" if pkg["status"] == "ready_return"
                          else "in_transfer")
            conn.execute("UPDATE packages SET status=?,current_cell_id=NULL,"
                         "updated_at=? WHERE id=?", (new_status, now(), pkg_id))
            conn.execute("INSERT INTO handovers(package_id,kind,from_party,to_party,"
                         "receiver_name,note,ts) VALUES(?,?,?,?,?,?,?)",
                         (pkg_id, "force_open", "cell:" + cell["id"], actor["id"],
                          actor["name"], f"approval#{approval_id}", now()))
            self.db.event(conn, pkg_id, actor["id"], "FORCE_RETRIEVE",
                          {"approval": approval_id, "next_status": new_status},
                          cell_id=cell["id"],
                          old_code=pkg["pickup_code"])

        self.db.tx(work)
        return {"retrieved": True, "cell": cell["id"],
                "door_kept_fault": True}

    # ============ 列表（公司隔离） ============
    def list_packages(self, actor, status=None, building_id=None, limit=200):
        sql = ("SELECT p.*, r.name rname, r.phone rphone, r.code rcode,"
               "c.code cell_code, l.name locker_name, b.name bname FROM packages p "
               "JOIN recipients r ON r.id=p.recipient_id "
               "LEFT JOIN cells c ON c.id=p.current_cell_id "
               "LEFT JOIN lockers l ON l.id=p.locker_id "
               "LEFT JOIN buildings b ON b.id=p.building_id WHERE 1=1 ")
        args = []
        if actor["role"] == "courier":
            sql += "AND p.company=? "
            args.append(actor["company"])
        if status:
            sql += "AND p.status=? "
            args.append(status)
        if building_id:
            sql += "AND p.building_id=? "
            args.append(building_id)
        sql += "ORDER BY p.updated_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.q(sql, args)]

    def package_detail(self, actor, pkg_id):
        pkg = self._pkg(pkg_id)
        self.require_company(actor, pkg)
        r = self.db.q1("SELECT * FROM recipients WHERE id=?", (pkg["recipient_id"],))
        events = self.db.events(pkg_id)
        transfers = self.transfer_chain(pkg_id)
        handovers = self.db.q("SELECT * FROM handovers WHERE package_id=? ORDER BY id",
                              (pkg_id,))
        notifs = self.db.q("SELECT id,kind,phone,content,status,retries,created_ts,"
                           "sent_ts,last_error FROM notifications WHERE package_id=? "
                           "ORDER BY id", (pkg_id,))
        return {"package": dict(pkg), "recipient": dict(r),
                "events": [dict(e) for e in events],
                "transfers": transfers,
                "handovers": [dict(h) for h in handovers],
                "notifications": [dict(n) for n in notifs]}

    def notifications_pending(self):
        return self.db.q("SELECT * FROM notifications WHERE status IN ('pending','failed')"
                         " AND retries<=max_retries ORDER BY id")

    def stats(self):
        def one(sql, a=()):
            return self.db.q1(sql, a)[0]
        return {
            "stored": one("SELECT COUNT(*) FROM packages WHERE status='stored'"),
            "on_shelf": one("SELECT COUNT(*) FROM packages WHERE status='on_shelf'"),
            "ready_return": one("SELECT COUNT(*) FROM packages WHERE status='ready_return'"),
            "sealed": one("SELECT COUNT(*) FROM packages WHERE status='sealed'"),
            "returned": one("SELECT COUNT(*) FROM packages WHERE status='returned'"),
            "picked_up": one("SELECT COUNT(*) FROM packages WHERE status='picked_up'"),
            "fault_cells": one("SELECT COUNT(*) FROM cells WHERE door_status='fault'"),
            "open_cells": one("SELECT COUNT(*) FROM cells WHERE door_status='open'"),
            "pending_approvals": one(
                "SELECT COUNT(*) FROM force_open_approvals WHERE status='pending'"),
        }
