"""系统测试：格口竞争、码失效、转移链、强开审批、公司隔离，
另含尺寸不符、关门回执、开门未关不可分配、超时退回、拒收凭证、
前台代取授权、通知重试。

直接对 Service 层运行，每个用例使用独立临时 SQLite 与凭证目录。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB
from app.services import Service, DomainError
from app import seed, workers

# 通知重试退避在测试中归零，立即重试
workers.RETRY_BACKOFF = [0, 0, 0]


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.dir, "t.db"))
        self.evidence = os.path.join(self.dir, "evidence")
        self.svc = Service(self.db, self.evidence)
        self.info = seed.seed(self.db, self.svc)
        self.sf = self.db.q1("SELECT * FROM couriers WHERE id='U-SF'")
        self.yt = self.db.q1("SELECT * FROM couriers WHERE id='U-YT'")
        self.jd = self.db.q1("SELECT * FROM couriers WHERE id='U-JD'")
        self.fd = self.db.q1("SELECT * FROM couriers WHERE id='U-FD'")
        # 第二名顺丰快递员，用于争用
        self.db.x("INSERT INTO couriers VALUES('U-SF2','顺丰速运','程凯二号',"
                  "'13900020009','courier')")
        self.sf2 = self.db.q1("SELECT * FROM couriers WHERE id='U-SF2'")

    def pkg(self, waybill):
        return self.db.q1("SELECT * FROM packages WHERE waybill=?", (waybill,))

    def free_cell(self, actor, pkg, bid="B2"):
        return self.svc.suggest(actor, pkg["id"], bid)["cell"]

    def register_sf(self, size="S", wb=None):
        wb = wb or f"SF-T{os.urandom(2).hex()}"
        return self.svc.register_package(
            self.sf, wb, "R-001", size,
            *seed.PKG_DIM[size])


class TestCellContention(Case):
    """两名快递员争用同一格口，只能一人获得锁。"""

    def test_only_one_wins(self):
        p1 = self.register_sf("S")
        p2 = self.register_sf("S", wb="SF-CONT2")
        cell = self.free_cell(self.sf, p1)
        self.assertIsNotNone(cell)

        a = self.svc.acquire_cell(self.sf, p1["id"], cell["id"])
        self.assertTrue(a["acquired"])
        # 第二人争用同一格口 → 拒绝
        with self.assertRaises(DomainError) as e:
            self.svc.acquire_cell(self.sf2, p2["id"], cell["id"])
        self.assertEqual(e.exception.code, "CELL_LOCKED")
        self.assertIn("已获得", str(e.exception))

        # 第一人关门回执成功，正式入柜
        r = self.svc.close_receipt(self.sf, p1["id"])
        self.assertTrue(r["stored"])
        # 格口已占用，第二人依旧无法使用
        with self.assertRaises(DomainError) as e2:
            self.svc.acquire_cell(self.sf2, p2["id"], cell["id"])
        self.assertEqual(e2.exception.code, "CELL_OCCUPIED")

    def test_open_door_not_assignable(self):
        """格口开门未关闭时不能分配。"""
        p1 = self.register_sf("S")
        cell = self.free_cell(self.sf, p1)
        self.svc.acquire_cell(self.sf, p1["id"], cell["id"])  # 门已开
        p2 = self.register_sf("S", wb="SF-OPEN2")
        with self.assertRaises(DomainError) as e:
            self.svc.acquire_cell(self.sf2, p2["id"], cell["id"])
        self.assertEqual(e.exception.code, "CELL_LOCKED")
        # 放弃后锁释放、门自动关上；改为手工把格口置为开门未关闭
        self.svc.abort_stow(self.sf, p1["id"])
        self.db.x("UPDATE cells SET door_status='open' WHERE id=?", (cell["id"],))
        with self.assertRaises(DomainError) as e2:
            self.svc.acquire_cell(self.sf, p1["id"], cell["id"])
        self.assertEqual(e2.exception.code, "DOOR_OPEN")

    def test_close_receipt_required(self):
        """只有开门、没有关门回执：包裹不算入柜。"""
        p = self.register_sf("M")
        cell = self.free_cell(self.sf, p)
        self.svc.acquire_cell(self.sf, p["id"], cell["id"])
        self.assertEqual(self.pkg(p["waybill"])["status"], "intransit")
        self.svc.close_receipt(self.sf, p["id"])
        self.assertEqual(self.pkg(p["waybill"])["status"], "stored")

    def test_size_mismatch_and_smallest_fit(self):
        """尺寸不符拒绝；小件匹配最小可用格口。"""
        pL = self.register_sf("L")
        # B2 找一个 S 空格口
        s_cell = self.db.q1(
            "SELECT c.* FROM cells c JOIN lockers l ON l.id=c.locker_id "
            "WHERE l.building_id='B2' AND c.size='S' AND c.door_status='closed' "
            "AND NOT EXISTS(SELECT 1 FROM packages p WHERE p.current_cell_id=c.id "
            "AND p.status='stored') LIMIT 1")
        with self.assertRaises(DomainError) as e:
            self.svc.acquire_cell(self.sf, pL["id"], s_cell["id"])
        self.assertEqual(e.exception.code, "SIZE_MISMATCH")
        # suggest 给大件的必须是 L 格
        plan = self.svc.suggest(self.sf, pL["id"], "B2")
        self.assertEqual(plan["cell"]["size"], "L")
        pS = self.register_sf("S", wb="SF-SMALL")
        self.assertEqual(self.svc.suggest(self.sf, pS["id"], "B2")["cell"]["size"], "S")


class TestCodeInvalidationAndChain(Case):
    """转移即作废旧码；转移链逐跳验证。"""

    def _stow_new_sf_in_b2(self, wb="SF-CHAIN", size="M"):
        p = self.register_sf(size, wb)
        cell1 = self.svc.suggest(self.sf, p["id"], "B2")["cell"]
        self.svc.acquire_cell(self.sf, p["id"], cell1["id"])
        r1 = self.svc.close_receipt(self.sf, p["id"])
        return p, cell1, r1["pickup_code"]

    def test_old_code_fails_after_transfer_to_shelf(self):
        p, cell1, code1 = self._stow_new_sf_in_b2()
        slot = self.db.q1(
            "SELECT * FROM shelf_slots WHERE building_id='B2' AND occupied_pkg IS NULL "
            "ORDER BY rack_no,level_no,slot_no LIMIT 1")
        r = self.svc.transfer(self.sf, p["id"], ("shelf", slot["id"]), "full")
        self.assertTrue(r["on_shelf"])
        self.assertEqual(r["old_code_invalid"], code1)
        self.assertNotEqual(r["pickup_code"], code1)
        # 旧码立即失效
        with self.assertRaises(DomainError) as e:
            self.svc.pickup(self.fd, code1)
        self.assertEqual(e.exception.code, "BAD_CODE")
        # 新码可取（货架交付）
        ok = self.svc.pickup(self.fd, r["pickup_code"], receiver_sign="本人")
        self.assertTrue(ok["picked_up"])
        self.assertEqual(self.pkg(p["waybill"])["status"], "picked_up")

    def test_transfer_chain_cell_cell_shelf(self):
        p, cell1, code1 = self._stow_new_sf_in_b2("SF-LINK")
        # cell1 -> 另一个柜内格口（B3 邻楼同组? B2/B1 同组1，B3 组2；
        # 直接调用 transfer 指定 B1 格口——B1 满仓，故用 M4 内第二个空格口）
        other = self.db.q1(
            "SELECT c.* FROM cells c WHERE locker_id='M4' AND id<>? "
            "AND door_status='closed' AND size='M' "
            "AND NOT EXISTS(SELECT 1 FROM packages p WHERE p.current_cell_id=c.id "
            "AND p.status IN ('stored','in_transfer','ready_return')) LIMIT 1",
            (cell1["id"],))
        self.assertIsNotNone(other, "M4 应有第二个空闲中格口")
        t1 = self.svc.transfer(self.sf, p["id"], ("cell", other["id"]), "full")
        self.assertTrue(t1["await_close"])
        c1 = self.svc.confirm_transfer_close(self.sf, p["id"])
        code2 = c1["pickup_code"]
        self.assertNotEqual(code2, code1)
        # code1 已失效
        with self.assertRaises(DomainError):
            self.svc.pickup(self.fd, code1)
        # 再转货架
        slot = self.db.q1(
            "SELECT * FROM shelf_slots WHERE building_id='B2' AND occupied_pkg IS NULL "
            "ORDER BY rack_no,level_no,slot_no LIMIT 1")
        t2 = self.svc.transfer(self.sf, p["id"], ("shelf", slot["id"]), "fault")
        code3 = t2["pickup_code"]
        chain = self.svc.transfer_chain(p["id"])
        self.assertEqual(len(chain), 2)
        self.assertEqual([x["old_code"] for x in chain], [code1, code2])
        self.assertEqual(chain[-1]["new_code"], code3)
        # 只有最新码可取
        with self.assertRaises(DomainError):
            self.svc.pickup(self.fd, code2)
        self.assertTrue(self.svc.pickup(self.fd, code3, receiver_sign="x")["picked_up"])

    def test_seed_chain_history(self):
        p = self.pkg("YT66000099")
        self.assertEqual(len(self.svc.transfer_chain(p["id"])), 2)
        for old in ("112233", "224466"):
            with self.assertRaises(DomainError):
                self.svc.pickup(self.fd, old)
        self.assertTrue(self.svc.pickup(self.fd, "446688", receiver_sign="x")["picked_up"])


class TestForceOpenApproval(Case):
    """强开：必须物业审批；门故障保留待修；取出后可转货架。"""

    def test_full_flow(self):
        pf = self.db.q1("SELECT * FROM packages WHERE id=?",
                        (self.info["fault_package"],))
        fault_cell = self.info["fault_cell"]
        self.assertEqual(self.svc._cell(fault_cell)["door_status"], "fault")

        # 快递员直接转走故障门内包裹 → 要求强开审批
        slot = self.db.q1(
            "SELECT * FROM shelf_slots WHERE building_id='B1' AND occupied_pkg IS NULL "
            "ORDER BY rack_no,level_no,slot_no LIMIT 1")
        with self.assertRaises(DomainError) as e:
            self.svc.transfer(self.sf, pf["id"], ("shelf", slot["id"]), "fault")
        self.assertEqual(e.exception.code, "NEED_FORCE_OPEN")

        # 种子里已有一张待审批单
        ap = self.db.q1("SELECT * FROM force_open_approvals WHERE package_id=? "
                        "AND status='pending'", (pf["id"],))
        # 无审批不能强开
        with self.assertRaises(DomainError) as e2:
            self.svc.force_retrieve(self.sf, pf["id"], ap["id"])
        self.assertEqual(e2.exception.code, "NO_APPROVAL")
        # 快递员不能审批
        with self.assertRaises(DomainError) as e3:
            self.svc.decide_force_open(self.sf, ap["id"], True)
        self.assertEqual(e3.exception.code, "FORBIDDEN")
        # 物业批准
        self.svc.decide_force_open(self.fd, ap["id"], True, "同意")
        r = self.svc.force_retrieve(self.sf, pf["id"], ap["id"])
        self.assertTrue(r["retrieved"])
        # 门保留故障态，维修前不可再分配
        self.assertEqual(self.svc._cell(fault_cell)["door_status"], "fault")
        with self.assertRaises(DomainError) as e4:
            other = self.register_sf("S", "SF-AFTER-FAULT")
            self.svc.acquire_cell(self.sf, other["id"], fault_cell)
        self.assertEqual(e4.exception.code, "DOOR_FAULT")
        # 物业维修结案后可正常使用（先把故障件转移走腾空关系）
        self.svc.transfer(self.sf, pf["id"], ("shelf", slot["id"]), "fault",
                          force_approval_id=ap["id"])
        self.svc.repair_cell(self.fd, fault_cell)
        self.assertEqual(self.svc._cell(fault_cell)["door_status"], "closed")


class TestCompanyIsolation(Case):
    """普通快递员不可查看/操作他公司包裹。"""

    def test_detail_and_actions_isolated(self):
        sf_pkg = self.pkg("SF55000044")  # 顺丰待退回
        with self.assertRaises(DomainError) as e:
            self.svc.package_detail(self.yt, sf_pkg["id"])
        self.assertEqual(e.exception.code, "FORBIDDEN")
        with self.assertRaises(DomainError):
            self.svc.extend(self.yt, sf_pkg["id"], 5)
        with self.assertRaises(DomainError):
            self.svc.reject_return(self.yt, sf_pkg["id"], "x")
        # 列表只含本公司
        waybills = {p["waybill"] for p in self.svc.list_packages(self.yt)}
        self.assertNotIn("SF55000044", waybills)
        self.assertTrue(any(w.startswith("YT") for w in waybills))
        # 扫码他司运单同样拒绝且不泄露状态
        with self.assertRaises(DomainError) as e2:
            self.svc.scan(self.yt, "SF55000044")
        self.assertEqual(e2.exception.code, "FORBIDDEN")

    def test_force_open_other_company_denied(self):
        with self.assertRaises(DomainError):
            self.svc.apply_force_open(self.yt, self.info["fault_cell"], "test")

    def test_property_sees_all(self):
        allp = self.svc.list_packages(self.fd)
        companies = {p["company"] for p in allp}
        self.assertGreaterEqual(len(companies), 3)

    def test_duplicate_scan_blocked(self):
        with self.assertRaises(DomainError) as e:
            self.svc.scan(self.yt, "YT66000099")
        # YT 公司扫本司货架件 → 重复扫码
        self.assertEqual(e.exception.code, "DUP_SHELF")
        # 顺丰扫一个已入柜顺丰件
        stored_sf = self.db.q1("SELECT waybill FROM packages WHERE company='顺丰速运'"
                               " AND status='stored' LIMIT 1")
        with self.assertRaises(DomainError) as e2:
            self.svc.scan(self.sf, stored_sf["waybill"])
        self.assertEqual(e2.exception.code, "DUP_STORED")


class TestOverdueReturnAndProxy(Case):
    """超时队列、退回交接、拒收凭证、前台代取授权、通知重试。"""

    def test_overdue_sweep_and_return(self):
        moved = self.svc.sweep_overdue()
        self.assertIn(self.pkg("YT77000011")["id"], moved)
        p = self.pkg("YT77000011")
        self.assertEqual(p["status"], "ready_return")
        # 超时后旧码无法取件
        with self.assertRaises(DomainError) as e:
            self.svc.pickup(self.fd, p["pickup_code"])
        self.assertEqual(e.exception.code, "OVERDUE")
        # 退回交接签收
        r = self.svc.confirm_return(self.yt, p["id"], "转运-冯师傅", "冯师傅")
        self.assertTrue(r["returned"])
        self.assertEqual(self.pkg("YT77000011")["status"], "returned")

    def test_reject_creates_evidence(self):
        p = self.pkg("SF55000044")
        before = len(os.listdir(self.evidence))
        r = self.svc.reject_return(self.sf, p["id"], "无退回标识")
        self.assertTrue(os.path.exists(r["evidence"]))
        self.assertEqual(len(os.listdir(self.evidence)), before + 1)
        # 包裹仍在待退回队列，并通知了收件人
        self.assertEqual(self.pkg("SF55000044")["status"], "ready_return")
        n = self.db.q1("SELECT * FROM notifications WHERE package_id=? AND kind='reject'",
                       (p["id"],))
        self.assertIsNotNone(n)

    def test_proxy_authorization_mismatch_and_success(self):
        p = self.pkg("YT66000099")  # 货架件，码 446688
        a = self.svc.create_authorization(self.fd, p["id"], "许助理", "13700090001")
        # 信息不一致 → 拒绝
        with self.assertRaises(DomainError) as e:
            self.svc.pickup(self.fd, "446688", auth_code=a["auth_code"],
                            proxy_name="陌生人", proxy_phone="13900000000")
        self.assertEqual(e.exception.code, "AUTH_MISMATCH")
        # 授权码错误
        with self.assertRaises(DomainError):
            self.svc.pickup(self.fd, "446688", auth_code="A000000",
                            proxy_name="许助理", proxy_phone="13700090001")
        # 一致 → 交付成功，授权一次性
        self.assertTrue(self.svc.pickup(
            self.fd, "446688", auth_code=a["auth_code"],
            proxy_name="许助理", proxy_phone="13700090001",
            receiver_sign="许助理")["picked_up"])
        with self.assertRaises(DomainError):
            self.svc.pickup(self.fd, "446688", auth_code=a["auth_code"],
                            proxy_name="许助理", proxy_phone="13700090001")

    def test_extension_delays_overdue(self):
        p = self.pkg("YT77000011")  # deadline 已过 2 小时：延期被拒
        with self.assertRaises(DomainError) as e:
            self.svc.extend(self.yt, p["id"], 12)
        self.assertEqual(e.exception.code, "ALREADY_OVERDUE")
        # 未超时件延期后不被扫入
        fresh = self.db.q1("SELECT * FROM packages WHERE status='stored' "
                           "AND datetime(deadline_at)>datetime('now') LIMIT 1")
        self.svc.extend(self.sf if fresh["company"] == "顺丰速运" else self.yt,
                        fresh["id"], 24)
        moved = self.svc.sweep_overdue()
        self.assertNotIn(fresh["id"], moved)

    def test_notification_retry(self):
        p = self.register_sf("S", "SF-RETRY")
        cell = self.free_cell(self.sf, p)
        self.svc.acquire_cell(self.sf, p["id"], cell["id"])
        self.svc.close_receipt(self.sf, p["id"])
        n = self.db.q1("SELECT * FROM notifications WHERE package_id=? AND kind='stored'",
                       (p["id"],))
        # 无失败注入：worker 首跳即送达
        w = workers.Worker(self.db, self.svc)
        w.retry_notifications()
        self.assertEqual(self.db.q1("SELECT status FROM notifications WHERE id=?",
                                    (n["id"],))["status"], "sent")
        # 再制造一条必失败两次的通知（拒收通知）
        rp = self.pkg("SF55000044")
        phone = self.db.q1("SELECT phone FROM recipients WHERE id=?",
                           (rp["recipient_id"],))["phone"]
        self.svc.sms.fail_next(phone, 2)
        self.svc.reject_return(self.sf, rp["id"], "再次拒收测试")
        n2 = self.db.q1("SELECT * FROM notifications WHERE package_id=? AND kind='reject'"
                        " ORDER BY id DESC LIMIT 1", (rp["id"],))
        w.retry_notifications()
        self.assertEqual(self.db.q1("SELECT status FROM notifications WHERE id=?",
                                    (n2["id"],))["status"], "failed")
        w.retry_notifications()  # 第二次仍失败（注入了 2 次失败）
        self.assertEqual(self.db.q1("SELECT status,retries FROM notifications WHERE id=?",
                                    (n2["id"],))["status"], "failed")
        w.retry_notifications()  # 退避归零，第三次送达
        self.assertEqual(self.db.q1("SELECT status FROM notifications WHERE id=?",
                                    (n2["id"],))["status"], "sent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
