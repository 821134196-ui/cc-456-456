"""种子数据：全部为虚构姓名、手机号与运单号。

启动时构造演示局面：
- B1 海辰国际A座：M1/M2/M3 全部满仓；M2 含故障门（件在柜内，已有强开申请）、
  开门未关闭格口、两个超时件；
- B2 海辰国际B座 M4：有空闲大/中格口（邻楼转移目的地）；
- 一件全新在途大包裹待投递（演示满仓→货架/邻楼连续路径）；
- 人工货架已有一条 cell→cell→shelf 转移链包裹（旧码全部作废）；
- 退回队列含一件超时待退回、一件拒收留存凭证；另有一件异常封存样例；
- 一条短信注入两次失败，演示通知重试。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from .db import now

# 物理尺寸（mm）：格口
CELL_DIM = {
    "S": (300, 110, 400),
    "M": (400, 220, 450),
    "L": (550, 320, 520),
}
# 包裹尺寸（确保放入同档）
PKG_DIM = {
    "S": (260, 90, 350),
    "M": (360, 180, 400),
    "L": (500, 280, 460),
}

RECIPIENTS = [
    ("R-001", "林一舟", "13800010001"),
    ("R-002", "苏晚晴", "13800010002"),
    ("R-003", "沈知行", "13800010003"),
    ("R-004", "顾南星", "13800010004"),
    ("R-005", "叶知秋", "13800010005"),
    ("R-006", "程嘉树", "13800010006"),
    ("R-007", "温言之", "13800010007"),
    ("R-008", "阮清禾", "13800010008"),
    ("R-009", "贺云峥", "13800010009"),
    ("R-010", "白露微", "13800010010"),
    ("R-011", "秦望舒", "13800010011"),
    ("R-012", "孟星回", "13800010012"),
]

COURIERS = [
    ("U-SF", "顺丰速运", "程凯", "13900020001", "courier"),
    ("U-YT", "圆通速递", "高磊", "13900020002", "courier"),
    ("U-JD", "京东物流", "韩松", "13900020003", "courier"),
    ("U-FD", "物业服务", "周敏", "13900030001", "property"),
    ("U-PM", "物业服务", "梁峥", "13900030002", "property"),
]

# (locker, size, count)
LAYOUT = {
    "M1": [("L", 3), ("M", 5), ("S", 8)],
    "M2": [("L", 2), ("M", 4), ("S", 6)],
    "M3": [("L", 2), ("M", 4), ("S", 4)],
    "M4": [("L", 2), ("M", 4), ("S", 6)],
    "M5": [("L", 2), ("M", 3), ("S", 5)],
}
LOCKERS = [
    ("M1", "B1", "A座一层大堂柜机组"),
    ("M2", "B1", "A座B1层快递间柜机组"),
    ("M3", "B1", "A座二层连廊柜机组"),
    ("M4", "B2", "B座一层大堂柜机组"),
    ("M5", "B3", "云璟A座大堂柜机组"),
]
BUILDINGS = [
    ("B1", "海辰国际A座", "海辰路 88 号 A 座", 1),
    ("B2", "海辰国际B座", "海辰路 88 号 B 座", 1),
    ("B3", "云璟中心A座", "云璟大道 16 号 A 座", 2),
    ("B4", "云璟中心B座", "云璟大道 16 号 B 座", 2),
]

COMPANIES = ["顺丰速运", "圆通速递", "京东物流"]


def _iso(hours_from_now):
    return (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
            ).isoformat(timespec="seconds")


def reset(db):
    for t in ("handovers", "notifications", "force_open_approvals",
              "pickup_authorizations", "transfers", "package_events",
              "packages", "shelf_slots", "cell_locks", "door_commands",
              "cells", "lockers", "buildings", "recipients", "couriers"):
        db.x(f"DELETE FROM {t}")


def seed(db, svc):
    reset(db)
    c = db.conn
    for b in BUILDINGS:
        c.execute("INSERT INTO buildings VALUES(?,?,?,?)", b)
    for u in COURIERS:
        c.execute("INSERT INTO couriers VALUES(?,?,?,?,?)", u)
    for r in RECIPIENTS:
        c.execute("INSERT INTO recipients(id,code,name,phone) VALUES(?,?,?,?)",
                  (r[0], r[0], r[1], r[2]))
    for lid, bid, name in LOCKERS:
        c.execute("INSERT INTO lockers(id,building_id,name,enabled) VALUES(?,?,?,1)",
                  (lid, bid, name))

    cells_of = {}
    seq = 0
    for lid, layout in LAYOUT.items():
        group = []
        for size, n in layout:
            w, h, d = CELL_DIM[size]
            for i in range(1, n + 1):
                seq += 1
                cid = f"C-{lid}-{seq:02d}"
                code = f"{size}{i:02d}"
                c.execute("INSERT INTO cells(id,locker_id,code,size,width_mm,"
                          "height_mm,depth_mm,door_status,seq) VALUES(?,?,?,?,?,?,?,"
                          "'closed',?)", (cid, lid, code, size, w, h, d, seq))
                group.append((cid, size))
        cells_of[lid] = group

    # ---- 货架：每楼 2 架 × 4 层 × 4 位（B1 为 A/B，B2 为 C，B3 为 D）----
    racks = {"B1": ["A", "B"], "B2": ["C"], "B3": ["D"], "B4": ["E"]}
    shelf_of = {}
    for bid, rs in racks.items():
        for rack in rs:
            for level in range(1, 5):
                for pos in range(1, 5):
                    sid = f"SHF-{rack}-{level:02d}-{pos:02d}"
                    c.execute("INSERT INTO shelf_slots VALUES(?,?,?,?,?,NULL)",
                              (sid, bid, rack, level, pos))
                    shelf_of[(bid, rack, level, pos)] = sid

    waybill_seq = [1000]

    def add_pkg(recipient_idx, size, company, status="intransit",
                cell=None, building=None, waybill=None, deadline_h=20,
                stored_h_ago=2, code=None, shelf=None, return_reason=None,
                sealed_reason=None, notify=True, actor="U-SF"):
        waybill_seq[0] += 1
        wb = waybill or f"{company[:2]}{waybill_seq[0]:08d}"
        w, h, d = PKG_DIM[size]
        rid = RECIPIENTS[recipient_idx][0]
        pid = f"PK-{secrets.token_hex(4)}"
        ts = now()
        stored_at = _iso(-stored_h_ago) if status in ("stored", "on_shelf",
                                                       "ready_return") else None
        deadline = _iso(deadline_h) if status in ("stored", "on_shelf",
                                                  "ready_return") else None
        locker = c.execute("SELECT locker_id FROM cells WHERE id=?",
                           (cell,)).fetchone()[0] if cell else None
        c.execute(
            "INSERT INTO packages(id,waybill,company,recipient_id,size,width_mm,"
            "height_mm,depth_mm,status,current_cell_id,shelf_id,shelf_slot,"
            "locker_id,building_id,pickup_code,code_seq,stored_at,deadline_at,"
            "return_reason,sealed_reason,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, wb, company, rid, size, w, h, d, status,
             cell, shelf, shelf, locker, building,
             code, (1 if code else 0), stored_at, deadline,
             return_reason, sealed_reason, ts, ts))
        db.event(c, pid, actor,
                 {"stored": "STORED", "on_shelf": "TRANSFER_SHELF",
                  "ready_return": "OVERDUE", "sealed": "SEALED"}.get(status, "REGISTER"),
                 {"seed": True}, cell_id=cell, new_code=code)
        if code and notify:
            content = f"【{company}】您的快递 {wb} 已入柜，取件码 {code}。"
            c.execute("INSERT INTO notifications(package_id,phone,content,status,"
                      "retries,max_retries,next_retry_at,created_ts,sent_ts,kind) "
                      "VALUES(?,?,?, 'sent',0,3,?,?,?, 'stored')",
                      (pid, RECIPIENTS[recipient_idx][2], content, ts, ts, ts))
        return pid, wb

    code_n = [100000]

    def next_code():
        code_n[0] += 137
        return f"{code_n[0] % 10**6:06d}"

    # ---- M1：16 格全满 ----
    for i, (cid, size) in enumerate(cells_of["M1"]):
        comp = COMPANIES[i % 3]
        actor = {"顺丰速运": "U-SF", "圆通速递": "U-YT",
                 "京东物流": "U-JD"}[comp]
        add_pkg(i % 12, size, comp, status="stored", cell=cid, building="B1",
                code=next_code(), actor=actor)

    # ---- M2：12 格。规划：9 件在柜（含 1 故障门内、2 超时）、1 空门打开、
    #      其余 2 格故障停用 ----
    m2 = cells_of["M2"]
    fault_loaded = m2[0]          # L 件，故障门
    overdue_cells = [m2[1], m2[4]]
    open_door_cell = m2[-1]
    fault_empty = [m2[-2], m2[-3]]
    stored_cells = [x for x in m2 if x not in [fault_loaded] + overdue_cells
                    + [open_door_cell] + fault_empty]
    for i, (cid, size) in enumerate(stored_cells):
        comp = COMPANIES[(i + 1) % 3]
        actor = {"顺丰速运": "U-SF", "圆通速递": "U-YT",
                 "京东物流": "U-JD"}[comp]
        add_pkg((i + 3) % 12, size, comp, status="stored", cell=cid,
                building="B1", code=next_code(), actor=actor)
    # 故障门内包裹（顺丰）
    pfid, pfw = add_pkg(3, fault_loaded[1], "顺丰速运", status="stored",
                        cell=fault_loaded[0], building="B1",
                        code=next_code(), actor="U-SF")
    c.execute("UPDATE cells SET door_status='fault' WHERE id=?",
              (fault_loaded[0],))
    c.execute("INSERT INTO door_commands(cell_id,action,result,detail,ts) "
              "VALUES(?,?,?,?,?)",
              (fault_loaded[0], "open", "failed", "door_fault：门锁电机无响应", now()))
    c.execute("INSERT INTO force_open_approvals(cell_id,package_id,reason,"
              "applicant,status,created_ts) VALUES(?,?,?,?,'pending',?)",
              (fault_loaded[0], pfid, "柜门电机故障，客户急取，申请物业强制开柜",
               "U-SF", now()))
    db.event(c, pfid, "U-SF", "DOOR_FAULT_REPORT",
             {"cell": fault_loaded[0], "seed": True}, cell_id=fault_loaded[0])
    # 超时件：时限已过 2 小时（worker 启动后扫入 ready_return）
    add_pkg(4, overdue_cells[0][1], "圆通速递", status="stored",
            cell=overdue_cells[0][0], building="B1", code=next_code(),
            deadline_h=-2, actor="U-YT", waybill="YT77000011")
    add_pkg(5, overdue_cells[1][1], "京东物流", status="stored",
            cell=overdue_cells[1][0], building="B1", code=next_code(),
            deadline_h=-2, actor="U-JD", waybill="JD77000022")
    # 开门未关闭（空门），不可分配
    c.execute("UPDATE cells SET door_status='open' WHERE id=?",
              (open_door_cell[0],))
    c.execute("INSERT INTO door_commands(cell_id,action,result,detail,ts) "
              "VALUES(?,?,?,?,?)",
              (open_door_cell[0], "open", "success", "ok", now()))
    for cid, _ in fault_empty:
        c.execute("UPDATE cells SET door_status='fault' WHERE id=?", (cid,))
        c.execute("INSERT INTO door_commands(cell_id,action,result,detail,ts) "
                  "VALUES(?,?,?,?,?)",
                  (cid, "open", "failed", "door_fault：门体变形待修", now()))

    # ---- M3：10 格全满 ----
    for i, (cid, size) in enumerate(cells_of["M3"]):
        comp = COMPANIES[i % 3]
        actor = {"顺丰速运": "U-SF", "圆通速递": "U-YT",
                 "京东物流": "U-JD"}[comp]
        add_pkg((i + 6) % 12, size, comp, status="stored", cell=cid,
                building="B1", code=next_code(), actor=actor)

    # ---- M4（邻楼 B2）：12 格中 6 件，留足空格 ----
    m4 = cells_of["M4"]
    occupied_m4 = {0, 3, 6, 8, 10, 11}
    for i, (cid, size) in enumerate(m4):
        if i in occupied_m4:
            comp = COMPANIES[i % 3]
            actor = {"顺丰速运": "U-SF", "圆通速递": "U-YT",
                     "京东物流": "U-JD"}[comp]
            add_pkg((i + 2) % 12, size, comp, status="stored", cell=cid,
                    building="B2", code=next_code(), actor=actor)

    # ---- M5（B3）：10 格中 4 件 ----
    for i, (cid, size) in enumerate(cells_of["M5"][:4]):
        add_pkg((i + 8) % 12, size, "京东物流", status="stored", cell=cid,
                building="B3", code=next_code(), actor="U-JD")

    # ---- 人工货架转移链：M1→M2→货架 A-01-02，旧码全部作废 ----
    chain_cell1 = cells_of["M1"][1][0]
    chain_cell2 = cells_of["M2"][5][0]
    # 从这两个格口移除之前填入的占用件（改成空置：删除包裹关系）
    for cid in (chain_cell1, chain_cell2):
        old = c.execute("SELECT id FROM packages WHERE current_cell_id=?",
                        (cid,)).fetchone()
        if old:
            c.execute("DELETE FROM package_events WHERE package_id=?", (old[0],))
            c.execute("DELETE FROM notifications WHERE package_id=?", (old[0],))
            c.execute("DELETE FROM packages WHERE id=?", (old[0],))
    slot_chain = shelf_of[("B1", "A", 1, 2)]
    c.execute("UPDATE shelf_slots SET occupied_pkg=? WHERE id=?",
              ("PK-CHAIN", slot_chain))
    chain_ts = [_iso(-26), _iso(-20), _iso(-18)]
    c.execute(
        "INSERT INTO packages(id,waybill,company,recipient_id,size,width_mm,"
        "height_mm,depth_mm,status,current_cell_id,shelf_id,shelf_slot,"
        "locker_id,building_id,pickup_code,code_seq,stored_at,deadline_at,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("PK-CHAIN", "YT66000099", "圆通速递", RECIPIENTS[6][0], "M",
         *PKG_DIM["M"], "on_shelf", None, slot_chain, slot_chain, None, "B1",
         "446688", 3, chain_ts[0], _iso(2), chain_ts[0], now()))
    c.execute("INSERT INTO transfers(package_id,from_type,from_ref,to_type,to_ref,"
              "reason,old_code,new_code,courier_id,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("PK-CHAIN", "cell", chain_cell1, "cell", chain_cell2, "full",
               "112233", "224466", "U-YT", chain_ts[1]))
    c.execute("INSERT INTO transfers(package_id,from_type,from_ref,to_type,to_ref,"
              "reason,old_code,new_code,courier_id,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("PK-CHAIN", "cell", chain_cell2, "shelf", slot_chain, "fault",
               "224466", "446688", "U-YT", chain_ts[2]))
    db.event(c, "PK-CHAIN", "U-YT", "STORED", {"seed": True},
             cell_id=chain_cell1, new_code="112233")
    db.event(c, "PK-CHAIN", "U-YT", "TRANSFER_STORED", {"seed": True},
             cell_id=chain_cell2, old_code="112233", new_code="224466")
    db.event(c, "PK-CHAIN", "U-YT", "TRANSFER_SHELF",
             {"rack": "A", "level": 1, "pos": 2}, old_code="224466",
             new_code="446688")
    for kd, content in (
            ("112233", "入柜取件码 112233"),
            ("224466", "包裹已转移，新取件码 224466，旧码 112233 已作废"),
            ("446688", "包裹转入人工货架 A架1层02位，货架取件码 446688，旧码 224466 已作废")):
        c.execute("INSERT INTO notifications(package_id,phone,content,status,"
                  "retries,max_retries,next_retry_at,created_ts,sent_ts,kind) "
                  "VALUES(?,?,?, 'sent',0,3,?,?,?, 'transferred')",
                  ("PK-CHAIN", RECIPIENTS[6][2],
                   f"【圆通速递】YT66000099 {content}",
                   chain_ts[0], chain_ts[2], chain_ts[2]))
    # 转移链腾出的 M1 格口补入新件，保持 M1 满仓
    add_pkg(11, "L", "京东物流", status="stored", cell=chain_cell1,
            building="B1", code=next_code(), actor="U-JD")

    # ---- 待退回件（超时，已在队列中；物理上占用一个 M4 格口）----
    ret_cell = m4[9]
    prid, prwb = add_pkg(8, ret_cell[1], "圆通速递", status="ready_return",
                         cell=ret_cell[0], building="B2", code=next_code(),
                         deadline_h=-6, return_reason="timeout",
                         actor="U-YT", waybill="YT55000033", notify=False)

    # ---- 退回拒收件（货架 B1 B-02-03，带凭证与交接记录）----
    rej_slot = shelf_of[("B1", "B", 2, 3)]
    rej_id, rej_wb = add_pkg(9, "S", "顺丰速运", status="ready_return",
                             building="B1", shelf=rej_slot,
                             return_reason="timeout", actor="U-SF",
                             waybill="SF55000044", notify=False)
    c.execute("UPDATE shelf_slots SET occupied_pkg=? WHERE id=?",
              (rej_id, rej_slot))
    ev_path = svc._save_evidence(
        {"waybill": rej_wb}, "return_reject",
        f"运单 {rej_wb}\n公司 顺丰速运\n拒收时间 {now()}\n"
        "拒收人 高磊(圆通速递代班)\n说明 运单外包装无退回标识，拒收待核实\n")
    c.execute("INSERT INTO handovers(package_id,kind,from_party,to_party,"
              "receiver_name,evidence_path,note,ts) VALUES(?,?,?,?,?,?,?,?)",
              (rej_id, "return_reject", "顺丰速运", "property", "程凯",
               ev_path, "外包装无退回标识，拒收待核实", now()))
    c.execute("UPDATE packages SET reject_note=? WHERE id=?",
              ("外包装无退回标识，拒收待核实", rej_id))

    # ---- 异常封存样例 ----
    seal_id, seal_wb = add_pkg(10, "M", "京东物流", status="sealed",
                               building="B1", sealed_reason="包裹破损渗液",
                               actor="U-FD", waybill="JD44000055",
                               notify=False)
    seal_path = svc._save_evidence(
        {"waybill": seal_wb}, "seal",
        f"运单 {seal_wb}\n封存时间 {now()}\n经办人 周敏\n原因 包裹破损渗液\n"
        "签名 周敏\n")
    c.execute("INSERT INTO handovers(package_id,kind,from_party,to_party,"
              "receiver_name,receiver_sign,evidence_path,note,ts) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (seal_id, "seal", "U-FD", "property_vault", "周敏", "周敏",
               seal_path, "包裹破损渗液", now()))

    # ---- 演示用在途大包裹（顺丰，B1 满仓 → 走连续路径转移）----
    demo_id, demo_wb = add_pkg(3, "L", "顺丰速运", status="intransit",
                               building="B1", waybill="SF90000001",
                               notify=False, actor="U-SF")
    c.execute("UPDATE packages SET current_cell_id=NULL WHERE id=?", (demo_id,))

    # ---- 一条注定重试两次才送达的短信（给超时件 YT77000011 的收件人）----
    svc.sms.fail_next(RECIPIENTS[4][2], 2)

    return {
        "demo_package": demo_id,
        "demo_waybill": demo_wb,
        "fault_cell": fault_loaded[0],
        "fault_package": pfid,
        "chain_package": "PK-CHAIN",
        "return_pending": prid,
        "return_reject": rej_id,
        "open_door_cell": open_door_cell[0],
    }
