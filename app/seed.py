"""演示种子数据：构造满仓柜机、柜门故障、超时/退回队列、转移链、
开门未关闭格口、短信失败待重试等场景。所有姓名、手机号、运单号均为虚构。

用法：python -m app.seed [--reset]
"""
import sys
from datetime import datetime, timedelta

from . import db as dbmod
from .adapters import SmsAdapter
from .services import LockerService, STORAGE_LIMIT_HOURS


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


def reset(conn):
    for t in ["evidence_file", "proxy_authorization", "force_open_request", "return_handoff",
              "door_receipt", "notification", "transfer_record", "package_event", "pickup_code",
              "package", "shelf_slot", "shelf", "compartment", "locker", "recipient",
              "courier_user", "building", "company"]:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


def seed(conn):
    now = datetime.now()
    H = lambda h: now + timedelta(hours=h)  # noqa

    # ---------- 公司 / 楼栋 / 人员（虚构） ----------
    companies = [("C-SF", "顺捷速运"), ("C-HD", "汇达快递")]
    conn.executemany("INSERT INTO company(id,name) VALUES(?,?)", companies)

    buildings = [("B-A", "云岸大厦A座", "yun-an"), ("B-B", "云岸大厦B座", "yun-an")]
    conn.executemany("INSERT INTO building(id,name,neighbor_group) VALUES(?,?,?)", buildings)

    users = [
        ("U-SF-1", "C-SF", "韦立", "courier"),
        ("U-SF-2", "C-SF", "韩松", "courier"),
        ("U-HD-1", "C-HD", "雷奔", "courier"),
        ("U-PROP-1", None, "沈砚", "property"),
        ("U-FD-1", None, "林晓棠", "frontdesk"),
    ]
    conn.executemany("INSERT INTO courier_user(id,company_id,name,role) VALUES(?,?,?,?)", users)

    # ---------- 收件人（代号 + 虚构手机号） ----------
    recipients = [
        ("R-01", "周先生(A-1207)", "13800010001"),
        ("R-02", "吴女士(A-0805)", "13800010002"),
        ("R-03", "郑先生(A-1601)", "13800010003"),
        ("R-04", "王女士(B-0902)", "13800010004"),
        ("R-05", "冯先生(B-2108)", "13800010005"),
        ("R-06", "蒋女士(A-0303)", "13800010006"),
        ("R-07", "沈先生(B-1506)", "13800010007"),
        ("R-08", "韩女士(A-1102)", "13800010008"),
    ]
    conn.executemany("INSERT INTO recipient(id,alias,phone) VALUES(?,?,?)", recipients)

    # ---------- 柜机与格口 ----------
    lockers = [("LK-A1", "B-A", "A座一层主柜"), ("LK-A2", "B-A", "A座二层柜"),
               ("LK-B1", "B-B", "B座一层柜")]
    conn.executemany("INSERT INTO locker(id,building_id,name) VALUES(?,?,?)", lockers)

    def add_compartments(locker_id, spec):
        rows = []
        seq = 0
        for size, n in spec:
            for i in range(1, n + 1):
                seq += 1
                code = f"{size}{seq:02d}"
                rows.append((f"{locker_id}-{code}", locker_id, code, size, seq))
        conn.executemany(
            "INSERT INTO compartment(id,locker_id,code,size,seq,status,door_status) VALUES(?,?,?,?,?,'free','closed')",
            rows,
        )
        return [r[0] for r in rows]

    a1 = add_compartments("LK-A1", [("L", 2), ("M", 4), ("S", 8)])   # 14，全部占满
    a2 = add_compartments("LK-A2", [("L", 1), ("M", 2), ("S", 3)])   # 6
    b1 = add_compartments("LK-B1", [("L", 1), ("M", 2), ("S", 4)])   # 7

    # ---------- 人工货架（架号/层号/位号高密度定位；Q 为异常封存架） ----------
    shelves = [("SH-A", "B-A", "A座人工货架"), ("SH-B", "B-B", "B座人工货架")]
    conn.executemany("INSERT INTO shelf(id,building_id,name) VALUES(?,?,?)", shelves)

    def add_slots(shelf_id, rack_specs):
        ids = []
        for rack, levels, positions in rack_specs:
            for lv in range(1, levels + 1):
                for pos in range(1, positions + 1):
                    sid = f"{shelf_id}-{rack}{lv}{pos}"
                    conn.execute(
                        "INSERT INTO shelf_slot(id,shelf_id,rack_no,level_no,pos_no,status) VALUES(?,?,?,?,?,'free')",
                        (sid, shelf_id, rack, lv, pos),
                    )
                    ids.append(sid)
        return ids

    add_slots("SH-A", [("A", 3, 4), ("B", 3, 4), ("Q", 2, 2)])
    add_slots("SH-B", [("A", 2, 4), ("Q", 1, 2)])

    # ---------- 包裹构造工具 ----------
    pkg_no = [0]

    def make_package(company, recipient, building, size, status, waybill,
                     comp_id=None, slot_id=None, stored_at=None, deadline=None,
                     delivered_by="U-SF-1", note=None):
        pkg_no[0] += 1
        pid = f"P-{pkg_no[0]:03d}"
        stored_iso = iso(stored_at) if stored_at else None
        dead_iso = iso(deadline) if deadline else None
        conn.execute(
            "INSERT INTO package(id,waybill,company_id,recipient_id,building_id,size,status,"
            "current_locker_id,current_compartment_id,current_shelf_slot_id,delivered_by,"
            "stored_at,deadline_at,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, waybill, company, recipient, building, size, status,
             ("LK-A1" if comp_id and comp_id in a1 else "LK-A2" if comp_id and comp_id in a2
              else "LK-B1" if comp_id else None),
             comp_id, slot_id, delivered_by, stored_iso, dead_iso, note, iso(now - timedelta(hours=12))),
        )
        return pid

    def occupy(comp_id, pid):
        conn.execute(
            "UPDATE compartment SET status='occupied', door_status='closed', occupant_package_id=? WHERE id=?",
            (pid, comp_id),
        )

    def store(pid, target_desc, code):
        conn.execute(
            "INSERT INTO pickup_code(package_id,code,status,issued_for,created_at) VALUES(?,?,'active',?,?)",
            (pid, code, target_desc, iso(now))
        )
        r = conn.execute("SELECT phone FROM recipient r JOIN package p ON p.recipient_id=r.id WHERE p.id=?",
                         (pid,)).fetchone()
        conn.execute(
            "INSERT INTO notification(package_id,ts,phone,text,status,attempts,receipt_id) "
            "VALUES(?,?,?,?,'sent',1,?)",
            (pid, iso(now), r["phone"], f"【商务楼快递柜】包裹已入柜 {target_desc}，取件码 {code}",
             f"SEED-{pid}")
        )
        conn.execute(
            "INSERT INTO package_event(package_id,ts,type,actor,detail) VALUES(?,?,?,?,?)",
            (pid, iso(now), "stored", "seed", f"入柜 {target_desc}，取件码 {code}")
        )

    # ---------- A1 满仓：14 个格口全部占用 ----------
    sf_n, hd_n = 9, 5
    for idx, comp_id in enumerate(a1):
        company = "C-SF" if idx < sf_n else "C-HD"
        n = idx + 1
        waybill = f"SF{n:06d}" if company == "C-SF" else f"HD{n:06d}"
        recipient = recipients[idx % len(recipients)][0]
        size = comp_id.split("-")[-1][0]
        pid = make_package(company, recipient, "B-A", size, "stored", waybill, comp_id=comp_id,
                           stored_at=now - timedelta(hours=10),
                           deadline=now - timedelta(hours=-STORAGE_LIMIT_HOURS[size] + 10),
                           delivered_by="U-SF-1" if company == "C-SF" else "U-HD-1")
        occupy(comp_id, pid)
        store(pid, f"云岸大厦A座-A座一层主柜-{comp_id.split('-')[-1]}", f"8{idx+1:05d}")

    # ---------- A2：2 占用 + 1 故障封存 + 1 开门未关闭 + 2 空闲 ----------
    p_fault = make_package("C-SF", "R-03", "B-A", "M", "sealed", "SF900101",
                           comp_id=a2[1], stored_at=now - timedelta(hours=30),
                           deadline=now - timedelta(hours=6))
    # 故障格口（含包裹，异常封存）
    conn.execute(
        "UPDATE compartment SET status='fault', door_status='fault', occupant_package_id=?, fault_note=? WHERE id=?",
        (p_fault, "锁舌不回弹，关门后红灯告警", a2[1]),
    )
    conn.execute(
        "INSERT INTO pickup_code(package_id,code,status,issued_for,created_at,voided_at,void_reason) "
        "VALUES(?,?,'void',?,?,?,?)",
        (p_fault, "700001", "A座二层柜-M02", iso(now - timedelta(hours=30)), iso(now - timedelta(hours=4)),
         "柜门故障封存")
    )
    conn.execute(
        "INSERT INTO package_event(package_id,ts,type,actor,detail) VALUES(?,?,?,?,?)",
        (p_fault, iso(now - timedelta(hours=4)), "sealed", "U-SF-1",
         "柜门故障异常封存：锁舌不回弹，等待物业强开审批")
    )
    conn.execute(
        "INSERT INTO force_open_request(package_id,compartment_id,ts,applicant,reason,status) "
        "VALUES(?,?,?,?,?,'pending')",
        (p_fault, a2[1], iso(now - timedelta(hours=3)), "U-SF-1",
         "格口故障且内有包裹，客户在前台等待取件，申请强制开柜")
    )
    import os
    ev_dir = dbmod.EVIDENCE_DIR
    os.makedirs(ev_dir, exist_ok=True)
    ev_path = os.path.join(ev_dir, f"fault_SF900101_seed.txt")
    with open(ev_path, "w", encoding="utf-8") as f:
        f.write("异常凭证（模拟/种子）\n类型: fault\n运单: SF900101\n说明: 锁舌不回弹\n")
    conn.execute(
        "INSERT INTO evidence_file(package_id,kind,ts,path,note) VALUES(?,?,?,?,?)",
        (p_fault, "fault", iso(now - timedelta(hours=4)), ev_path, "锁舌不回弹，关门后红灯告警")
    )

    p_a2l = make_package("C-HD", "R-06", "B-A", "L", "stored", "HD200202",
                         comp_id=a2[0], stored_at=now - timedelta(hours=5),
                         deadline=H(19), delivered_by="U-HD-1")
    occupy(a2[0], p_a2l)
    store(p_a2l, "云岸大厦A座-A座二层柜-L01", "620202")

    p_a2s = make_package("C-SF", "R-08", "B-A", "S", "stored", "SF200203",
                         comp_id=a2[5], stored_at=now - timedelta(hours=2),
                         deadline=H(46))
    occupy(a2[5], p_a2s)
    store(p_a2s, "云岸大厦A座-A座二层柜-S06", "530203")

    # 开门未关闭：该格口不可被分配
    conn.execute("UPDATE compartment SET status='free', door_status='open' WHERE id=?", (a2[3],))
    conn.execute(
        "INSERT INTO door_receipt(compartment_id,ts,action,result,detail) VALUES(?,?,?,?,?)",
        (a2[3], iso(now - timedelta(minutes=8)), "open", "success", "弹门后未收到关门回执，门保持打开")
    )

    # ---------- B1：1 超时（将自动入退回队列，短信前两次失败）+ 1 正常 ----------
    p_ret1 = make_package("C-SF", "R-05", "B-B", "L", "overdue", "SF300301",
                          comp_id=b1[0], stored_at=now - timedelta(hours=50),
                          deadline=now - timedelta(hours=26))
    occupy(b1[0], p_ret1)
    store(p_ret1, "云岸大厦B座-B座一层柜-L01", "410301")
    conn.execute(
        "INSERT INTO package_event(package_id,ts,type,actor,detail) VALUES(?,?,?,?,?)",
        (p_ret1, iso(now - timedelta(hours=2)), "overdue", "system", "超过存放时限仍未取")
    )
    SmsAdapter.inject_failure("SF300301", 2)

    p_b1m = make_package("C-HD", "R-04", "B-B", "M", "stored", "HD300302",
                         comp_id=b1[2], stored_at=now - timedelta(hours=6),
                         deadline=H(30), delivered_by="U-HD-1")
    occupy(b1[2], p_b1m)
    store(p_b1m, "云岸大厦B座-B座一层柜-M03", "320302")

    # ---------- 货架 A：1 转移链包裹（A1→B1→货架，两次转移）+ 1 超时待退回 ----------
    p_chain = make_package("C-SF", "R-02", "B-A", "S", "on_shelf", "SF400401",
                           slot_id="SH-A-A11", stored_at=now - timedelta(hours=20),
                           deadline=H(28))
    conn.execute("UPDATE shelf_slot SET status='occupied', occupant_package_id=? WHERE id=?",
                 (p_chain, "SH-A-A11"))
    conn.execute(
        "INSERT INTO pickup_code(package_id,code,status,issued_for,created_at,voided_at,void_reason) "
        "VALUES(?,?,'void',?,?,?,?)",
        (p_chain, "110401", "A座一层主柜-S11", iso(now - timedelta(hours=20)),
         iso(now - timedelta(hours=12)), "转移柜机(full)")
    )
    conn.execute(
        "INSERT INTO pickup_code(package_id,code,status,issued_for,created_at,voided_at,void_reason) "
        "VALUES(?,?,'void',?,?,?,?)",
        (p_chain, "120402", "B座一层柜-S06", iso(now - timedelta(hours=12)),
         iso(now - timedelta(hours=6)), "转移货架(full)")
    )
    conn.execute(
        "INSERT INTO pickup_code(package_id,code,status,issued_for,created_at) VALUES(?,?,'active',?,?)",
        (p_chain, "130403", "云岸大厦A座-A座人工货架 A架1层1位", iso(now - timedelta(hours=6)))
    )
    conn.execute(
        "INSERT INTO transfer_record(package_id,ts,actor,reason,from_kind,from_target,to_kind,to_target,old_code,new_code) "
        "VALUES(?,?,?, 'full','locker',?, 'locker',?, ?,?)",
        (p_chain, iso(now - timedelta(hours=12)), "U-SF-1", a1[10], b1[5], "110401", "120402")
    )
    conn.execute(
        "INSERT INTO transfer_record(package_id,ts,actor,reason,from_kind,from_target,to_kind,to_target,old_code,new_code) "
        "VALUES(?,?,?, 'full','locker',?, 'shelf',?, ?,?)",
        (p_chain, iso(now - timedelta(hours=6)), "U-SF-1", b1[5], "SH-A-A11", "120402", "130403")
    )
    for ts_off, typ, detail in [
        (-20, "stored", "首次入柜 A座一层主柜，码 110401"),
        (-12, "transfer", "A1满仓转入邻楼B1，旧码110401作废，新码120402"),
        (-6, "transfer", "B1调柜转入同楼人工货架，旧码120402作废，新码130403"),
    ]:
        conn.execute(
            "INSERT INTO package_event(package_id,ts,type,actor,detail) VALUES(?,?,?,?,?)",
            (p_chain, iso(now + timedelta(hours=ts_off)), typ, "U-SF-1", detail)
        )

    p_ret2 = make_package("C-HD", "R-07", "B-A", "M", "overdue", "HD500501",
                          slot_id="SH-A-B23", stored_at=now - timedelta(hours=60),
                          deadline=now - timedelta(hours=30), delivered_by="U-HD-1")
    conn.execute("UPDATE shelf_slot SET status='occupied', occupant_package_id=? WHERE id=?",
                 (p_ret2, "SH-A-B23"))
    conn.execute(
        "INSERT INTO pickup_code(package_id,code,status,issued_for,created_at,voided_at,void_reason) "
        "VALUES(?,?,'void',?,?,?,?)",
        (p_ret2, "210501", "A座人工货架", iso(now - timedelta(hours=60)),
         iso(now - timedelta(hours=6)), "超时退回")
    )
    conn.execute(
        "INSERT INTO return_handoff(package_id,ts,status,reason,actor) VALUES(?,?,'pending','超时无人取','system')",
        (p_ret2, iso(now - timedelta(hours=6)))
    )

    # ---------- 待入柜：满仓转移演示（SF）+ 错投演示（HD，目标B座） ----------
    p_in1 = make_package("C-SF", "R-01", "B-A", "S", "intake", "SF600601",
                         note="演示：在满仓的A1扫码 → 转人工货架/邻楼")
    p_in2 = make_package("C-HD", "R-04", "B-B", "M", "intake", "HD600602",
                         note="演示：在A座扫码触发错投；HD快递员看不到SF包裹")

    conn.commit()
    return {
        "full_locker": "LK-A1",
        "fault_compartment": a2[1],
        "door_open_compartment": a2[3],
        "intake_waybills": ["SF600601", "HD600602"],
        "overdue_waybill_retry_sms": "SF300301",
        "pending_return_package": p_ret2,
        "transfer_chain_package": p_chain,
    }


def main(reset_data="--reset" in sys.argv):
    dbmod.init_db()
    conn = dbmod.get_conn()
    try:
        if reset_data:
            reset(conn)
        elif conn.execute("SELECT COUNT(*) c FROM company").fetchone()["c"]:
            print("数据库已有数据，跳过；如需重建请加 --reset")
            return
        info = seed(conn)
        print("种子数据已准备：")
        for k, v in info.items():
            print(f"  - {k}: {v}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
