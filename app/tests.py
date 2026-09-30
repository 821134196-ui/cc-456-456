"""端到端测试：格口竞争、码失效、转移链、强开审批、公司隔离等核心规则。

运行：python -m unittest app.tests -v   （或 ./run.sh test）
使用临时 SQLite 库与临时凭证目录，不依赖运行中的服务。
"""
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

from . import db as dbmod
from .services import LockerService, ServiceError
from .adapters import SmsAdapter
from .scheduler import Scheduler
from . import seed as seedmod


class LockerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SMART_LOCKER_DB"] = os.path.join(self.tmp.name, "test.db")
        dbmod.EVIDENCE_DIR = os.path.join(self.tmp.name, "evidence")
        import app.services as svcmod
        svcmod.EVIDENCE_DIR = dbmod.EVIDENCE_DIR
        dbmod.init_db()
        self.conn = dbmod.get_conn()
        SmsAdapter.reset_failure()
        seedmod.reset(self.conn)
        self.info = seedmod.seed(self.conn)  # seed 会为 SF300301 注入短信前两次失败

    def tearDown(self):
        self.conn.close()
        SmsAdapter.reset_failure()
        self.tmp.cleanup()

    def svc(self, user_id=None):
        actor = self.conn.execute("SELECT * FROM courier_user WHERE id=?", (user_id,)).fetchone() if user_id else None
        return LockerService(self.conn, actor)

    def add_intake(self, waybill, company="C-SF", building="B-A", size="S", recipient="R-01"):
        pid = f"T-{waybill}"
        self.conn.execute(
            "INSERT INTO package(id,waybill,company_id,recipient_id,building_id,size,status,created_at) "
            "VALUES(?,?,?,?,?,?, 'intake',?)",
            (pid, waybill, company, recipient, building, size, dbmod.now_iso()),
        )
        self.conn.commit()
        return pid

    def comp(self, cid):
        return self.conn.execute("SELECT * FROM compartment WHERE id=?", (cid,)).fetchone()

    # ---------- 1. 格口竞争：两人争一个格口，只能一人获得锁 ----------
    def test_compartment_race_only_one_wins(self):
        # LK-A2 恰好只剩一个可分配的小格口（另有一个门未关闭、一个故障）
        self.add_intake("TQ000001")
        self.add_intake("TQ000002")
        results = []
        barrier = threading.Barrier(2)

        def worker(waybill):
            conn = dbmod.get_conn()
            try:
                s = LockerService(conn, conn.execute(
                    "SELECT * FROM courier_user WHERE id='U-SF-1'").fetchone() if waybill == "TQ000001"
                    else conn.execute("SELECT * FROM courier_user WHERE id='U-SF-2'").fetchone())
                barrier.wait(2)
                try:
                    r = s.allocate(waybill, "LK-A2")
                    results.append(("ok", r["compartment_id"]))
                except ServiceError as e:
                    results.append(("err", e.code))
                conn.commit()
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=("TQ000001",))
        t2 = threading.Thread(target=worker, args=("TQ000002",))
        t1.start(); t2.start(); t1.join(); t2.join()

        oks = [r for r in results if r[0] == "ok"]
        errs = [r for r in results if r[0] == "err"]
        self.assertEqual(len(oks), 1, f"应有且仅有一人抢到格口: {results}")
        self.assertEqual(len(errs), 1)
        self.assertIn(errs[0][1], (409, 503))  # 409 原子抢占失败 / 503 已无适配格口
        winner_comp = self.comp(oks[0][1])
        self.assertEqual(winner_comp["status"], "allocating")
        self.assertEqual(winner_comp["door_status"], "open")

    # ---------- 2. 开门未关闭的格口不能分配 ----------
    def test_open_door_compartment_not_allocatable(self):
        # a2[3] 在种子里处于 door_status=open；无论如何不应被匹配
        open_comp = self.info["door_open_compartment"]
        self.add_intake("TD000001")
        r = self.svc("U-SF-1").allocate("TD000001", "LK-A2")
        self.assertNotEqual(r["compartment_id"], open_comp)
        self.assertEqual(self.comp(open_comp)["door_status"], "open")

    # ---------- 3. 关门回执成功才算入柜 ----------
    def test_close_receipt_required(self):
        self.add_intake("TC000001")
        s = self.svc("U-SF-1")
        a = s.allocate("TC000001", "LK-A2")
        pkg = self.conn.execute("SELECT * FROM package WHERE waybill='TC000001'").fetchone()
        self.assertEqual(pkg["status"], "intake")  # 开门后仍不算入柜
        self.assertIsNone(s._active_code(pkg["id"]))
        res = s.door_close_receipt(pkg["id"], a["alloc_token"])
        pkg = self.conn.execute("SELECT * FROM package WHERE waybill='TC000001'").fetchone()
        self.assertEqual(pkg["status"], "stored")
        self.assertEqual(self.comp(a["compartment_id"])["status"], "occupied")
        self.assertEqual(res["pickup_code"], s._active_code(pkg["id"])["code"])
        # 通知已排队
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM notification WHERE package_id=?", (pkg["id"],)).fetchone())

    # ---------- 4. 码失效：转移后旧码作废，旧码取件被拒，新码可用 ----------
    def test_code_void_on_transfer_chain(self):
        s = self.svc("U-SF-1")
        # 满仓 A1 → 货架
        with self.assertRaises(ServiceError) as cm:
            s.allocate("SF600601", "LK-A1")
        self.assertEqual(cm.exception.code, 503)
        pkg = self.conn.execute("SELECT * FROM package WHERE waybill='SF600601'").fetchone()
        t1 = s.transfer_to_shelf(pkg["id"], "SH-A", reason="full")
        code1 = t1["new_code"]
        # 货架 → A2 柜机（开门），关门回执后拿到新码
        t2 = s.transfer_to_locker(pkg["id"], "LK-A2", reason="full")
        self.assertEqual(t2["old_code"], code1)
        res = s.door_close_receipt(pkg["id"], t2["alloc_token"])
        code2 = res["pickup_code"]
        self.assertNotEqual(code1, code2)

        c1 = self.conn.execute("SELECT * FROM pickup_code WHERE code=?", (code1,)).fetchone()
        self.assertEqual(c1["status"], "void")
        c2 = self.conn.execute("SELECT * FROM pickup_code WHERE code=?", (code2,)).fetchone()
        self.assertEqual(c2["status"], "active")

        # 旧码取件 → 410 已失效
        guest = LockerService(self.conn, None)
        with self.assertRaises(ServiceError) as cm:
            guest.pickup_verify(code1)
        self.assertEqual(cm.exception.code, 410)
        self.assertTrue(cm.exception.extra.get("void"))

        # 新码 + 正确手机尾号可取
        r = guest.pickup_verify(code2, phone_last4="0001")
        self.assertTrue(r["ok"])
        guest.pickup_complete(pkg["id"])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM package WHERE id=?", (pkg["id"],)).fetchone()["status"], "picked")

        # 转移记录成链
        transfers = self.conn.execute(
            "SELECT * FROM transfer_record WHERE package_id=? ORDER BY id", (pkg["id"],)).fetchall()
        self.assertEqual(len(transfers), 2)
        self.assertEqual([t["to_kind"] for t in transfers], ["shelf", "locker"])

    # ---------- 5. 种子转移链：两次转移、三个码（两废一活） ----------
    def test_seed_transfer_chain(self):
        pkg = self.conn.execute("SELECT * FROM package WHERE id=?", (self.info["transfer_chain_package"],)).fetchone()
        self.assertEqual(pkg["status"], "on_shelf")
        codes = self.conn.execute("SELECT * FROM pickup_code WHERE package_id=? ORDER BY id", (pkg["id"],)).fetchall()
        self.assertEqual([c["status"] for c in codes], ["void", "void", "active"])
        self.assertEqual(codes[2]["code"], "130403")
        transfers = self.conn.execute(
            "SELECT to_kind FROM transfer_record WHERE package_id=? ORDER BY id", (pkg["id"],)).fetchall()
        self.assertEqual([t["to_kind"] for t in transfers], ["locker", "shelf"])

    # ---------- 6. 强开审批：快递员不能审批，未批准不能执行 ----------
    def test_force_open_approval_flow(self):
        fault_comp = self.info["fault_compartment"]
        courier = self.svc("U-SF-2")
        prop = self.svc("U-PROP-1")

        req = courier.force_open_apply(fault_comp, "测试：客户等待取件")
        rid = req["request_id"]

        # 快递员/前台审批被拒
        with self.assertRaises(ServiceError) as cm:
            courier.force_open_decide(rid, True)
        self.assertEqual(cm.exception.code, 403)

        # 未批准直接执行 → 403
        with self.assertRaises(ServiceError) as cm:
            courier.force_open_execute(rid)
        self.assertEqual(cm.exception.code, 403)

        # 物业批准 → 执行 → 取件完成
        prop.force_open_decide(rid, True, "同意")
        r = courier.force_open_execute(rid)
        pkg_id = r["package_id"]
        # 强开后原码全部作废
        self.assertFalse(self.conn.execute(
            "SELECT 1 FROM pickup_code WHERE package_id=? AND status='active'", (pkg_id,)).fetchone())
        courier.pickup_complete(pkg_id)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM package WHERE id=?", (pkg_id,)).fetchone()["status"], "picked")

        # 驳回路径：再来一单申请
        req2 = courier.force_open_apply(fault_comp, "第二次申请")
        prop.force_open_decide(req2["request_id"], False, "证据不足")
        with self.assertRaises(ServiceError) as cm:
            courier.force_open_execute(req2["request_id"])
        self.assertEqual(cm.exception.code, 403)

    # ---------- 7. 公司隔离：快递员看不到/动不了他司包裹 ----------
    def test_company_isolation(self):
        hd = self.svc("U-HD-1")   # 汇达快递
        sf_pkg = self.conn.execute("SELECT * FROM package WHERE waybill='SF600601'").fetchone()
        # 详情 403
        with self.assertRaises(ServiceError) as cm:
            hd.package_detail(sf_pkg["id"])
        self.assertEqual(cm.exception.code, 403)
        # 扫码 403
        with self.assertRaises(ServiceError) as cm:
            hd.scan_package("SF600601", "LK-A1")
        self.assertEqual(cm.exception.code, 403)
        # 列表只含本公司
        items = hd.list_packages()
        self.assertTrue(items)
        self.assertTrue(all(i["company_id"] == "C-HD" for i in items))
        # 物业可见全部
        prop_items = self.svc("U-PROP-1").list_packages()
        companies = {i["company_id"] for i in prop_items}
        self.assertEqual(companies, {"C-SF", "C-HD"})

    # ---------- 8. 尺寸不符：重新匹配更大格口 ----------
    def test_size_mismatch_reallocate(self):
        # B 座 B1：小包裹先分进小格口，实际上是中件 → 改配中格口
        pid = self.add_intake("TM000001", building="B-B", size="S", recipient="R-04")
        s = self.svc("U-SF-1")
        a = s.allocate("TM000001", "LK-B1")
        small_comp = self.comp(a["compartment_id"])
        self.assertEqual(small_comp["size"], "S")
        a2 = s.size_mismatch(pid, "M")
        mid_comp = self.comp(a2["compartment_id"])
        self.assertEqual(mid_comp["size"], "M")
        # 原小格口已释放
        self.assertEqual(self.comp(small_comp["id"])["status"], "free")
        s.door_close_receipt(pid, a2["alloc_token"])
        self.assertEqual(self.conn.execute(
            "SELECT status,size FROM package WHERE id=?", (pid,)).fetchone()["size"], "M")

    # ---------- 9. 错投：跨楼不能入柜，可登记凭证 ----------
    def test_misdelivery(self):
        s = self.svc("U-HD-1")
        r = s.scan_package("HD600602", "LK-A1")  # 目标 B-B，扫在 A 座
        self.assertTrue(r["misdirected"])
        with self.assertRaises(ServiceError) as cm:
            s.allocate("HD600602", "LK-A1")
        self.assertEqual(cm.exception.code, 400)
        pkg = self.conn.execute("SELECT * FROM package WHERE waybill='HD600602'").fetchone()
        res = s.mark_misdirected(pkg["id"], "扫错楼栋")
        self.assertTrue(os.path.isfile(res["evidence"]))
        # 直接转投正确楼栋 B1 可开门
        t = s.transfer_to_locker(pkg["id"], "LK-B1", reason="misdelivery")
        self.assertTrue(t["need_close_receipt"])

    # ---------- 10. 前台代取必须核对授权 ----------
    def test_proxy_authorization(self):
        pkg = self.conn.execute("SELECT * FROM package WHERE waybill='HD300302'").fetchone()
        fd = self.svc("U-FD-1")
        # 手机尾号错误
        with self.assertRaises(ServiceError) as cm:
            fd.pickup_verify("320302", phone_last4="9999")
        self.assertEqual(cm.exception.code, 403)
        # 无授权码代取（proxy_code 传空即按本人核尾号）；错误授权码 → 403
        with self.assertRaises(ServiceError) as cm:
            fd.pickup_verify("320302", proxy_code="0000")
        self.assertTrue(cm.exception.extra.get("proxy_invalid"))

        auth = fd.create_proxy_authorization(pkg["id"], "陈默")
        # 姓名不符
        with self.assertRaises(ServiceError) as cm:
            fd.pickup_verify("320302", proxy_code=auth["auth_code"], proxy_name="别人")
        self.assertEqual(cm.exception.code, 403)
        r = fd.pickup_verify("320302", proxy_code=auth["auth_code"], proxy_name="陈默")
        self.assertEqual(r["mode"], "proxy")
        fd.pickup_complete(pkg["id"], proxy_auth_id=r["proxy_auth_id"])
        # 授权一次性，已使用
        used = self.conn.execute("SELECT status FROM proxy_authorization WHERE id=?",
                                 (r["proxy_auth_id"],)).fetchone()
        self.assertEqual(used["status"], "used")

    # ---------- 11. 重复扫码 ----------
    def test_duplicate_scan(self):
        s = self.svc("U-SF-1")
        # 已入柜运单再次扫码 → 409（第一次扫 intake 单只是查询，不改状态）
        with self.assertRaises(ServiceError) as cm:
            s.scan_package("SF000001", "LK-A1")
        self.assertEqual(cm.exception.code, 409)
        self.assertEqual(cm.exception.extra["status"], "stored")
        # intake 运单可正常扫描
        r = s.scan_package("SF600601", "LK-A1")
        self.assertEqual(r["waybill"], "SF600601")

    # ---------- 12. 超时 → 退回入队 → 签收 / 拒收封存 + 短信重试 ----------
    def test_overdue_return_and_sms_retry(self):
        # SF300301 已超时 26h（超过 24h 宽限），种子设定短信前 2 次发送失败
        sch = Scheduler(run_immediately=False)
        stats = sch.tick_once()   # 入队：第 1 次发送失败，tick 末尾重试第 2 次仍失败
        self.assertGreaterEqual(stats["return"], 1)
        sch.tick_once()           # 下一周期重试：第 3 次送达
        self.conn.commit()        # 结束本连接读事务，读取调度器在独立连接中的写入
        rh = self.conn.execute(
            "SELECT * FROM return_handoff WHERE package_id=(SELECT id FROM package WHERE waybill='SF300301') "
            "AND status='pending'").fetchone()
        self.assertIsNotNone(rh)
        # 短信经重试后最终送达，attempts=3（前两次模拟失败）
        notif = self.conn.execute(
            "SELECT * FROM notification WHERE package_id=(SELECT id FROM package WHERE waybill='SF300301') "
            "AND text LIKE '%退回%' ORDER BY id DESC").fetchone()
        self.assertEqual(notif["status"], "sent")
        self.assertEqual(notif["attempts"], 3)
        self.assertIsNotNone(notif["receipt_id"])

        # SF 快递员签收本公司退回件
        s = self.svc("U-SF-1")
        s.handover_return(rh["id"], "仓管贺舟")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM package WHERE waybill='SF300301'").fetchone()["status"], "returned")

        # HD 超时件走拒收 → 封存 Q 架 + 凭证
        rh2 = self.conn.execute(
            "SELECT * FROM return_handoff WHERE package_id=(SELECT id FROM package WHERE waybill='HD500501')").fetchone()
        hd = self.svc("U-HD-1")
        r = hd.reject_return(rh2["id"], "运单污损")
        self.assertTrue(os.path.isfile(r["evidence"]))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM package WHERE waybill='HD500501'").fetchone()["status"], "sealed")
        slot = self.conn.execute(
            "SELECT rack_no FROM shelf_slot WHERE occupant_package_id=(SELECT id FROM package WHERE waybill='HD500501')"
        ).fetchone()
        self.assertEqual(slot["rack_no"], "Q")

    # ---------- 13. 公司隔离延伸：不能签收他司退回件 ----------
    def test_cannot_handover_other_company_return(self):
        rh2 = self.conn.execute(
            "SELECT * FROM return_handoff WHERE package_id=(SELECT id FROM package WHERE waybill='HD500501')").fetchone()
        sf = self.svc("U-SF-1")
        with self.assertRaises(ServiceError) as cm:
            sf.handover_return(rh2["id"], "韦立")
        self.assertEqual(cm.exception.code, 403)

    # ---------- 14. 收件人延期次数受限 ----------
    def test_extension_limit(self):
        pkg = self.conn.execute("SELECT * FROM package WHERE waybill='SF300301'").fetchone()
        s = self.svc("U-FD-1")
        s.request_extension(pkg["id"], 24)
        self.assertEqual(self.conn.execute(
            "SELECT ext_count FROM package WHERE id=?", (pkg["id"],)).fetchone()["ext_count"], 1)
        with self.assertRaises(ServiceError) as cm:
            s.request_extension(pkg["id"], 24)
        self.assertEqual(cm.exception.code, 409)

    # ---------- 15. 故障上报：内有包裹自动封存并写凭证 ----------
    def test_fault_report_seals_package(self):
        # LK-B1 M03 内有 HD300302
        comp = self.conn.execute("SELECT * FROM compartment WHERE id='LK-B1-M03'").fetchone()
        s = self.svc("U-HD-1")
        r = s.report_compartment_fault(comp["id"], "门关不严")
        self.assertEqual(r["sealed_package"], comp["occupant_package_id"])
        pkg = self.conn.execute("SELECT status FROM package WHERE id=?",
                                (comp["occupant_package_id"],)).fetchone()
        self.assertEqual(pkg["status"], "sealed")
        ev = self.conn.execute("SELECT path FROM evidence_file WHERE package_id=?",
                               (comp["occupant_package_id"],)).fetchone()
        self.assertTrue(os.path.isfile(ev["path"]))
        # 原取件码已作废
        self.assertFalse(self.conn.execute(
            "SELECT 1 FROM pickup_code WHERE package_id=? AND status='active'",
            (comp["occupant_package_id"],)).fetchone())


if __name__ == "__main__":
    unittest.main()
