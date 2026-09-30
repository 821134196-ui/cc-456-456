"""模拟硬件/通道适配器：门锁控制、门状态回执、短信网关。

- LockAdapter：控制格口门开关并生成回执。故障格口开门会失败；
  支持测试钩子模拟“门打开后未关闭”。
- SmsAdapter：模拟短信下发，可按运单号注入临时失败，用于通知重试链路验证。
所有数据为本地模拟，不触达真实设备或运营商。
"""
import os
import re
import json
import time
from datetime import datetime, timedelta

from . import db as dbmod


class LockAdapter:
    """模拟电子门锁。compartment.door_status: closed/open/fault。"""

    def __init__(self, conn):
        self.conn = conn

    def _log(self, comp_id, action, result, detail=""):
        self.conn.execute(
            "INSERT INTO door_receipt(compartment_id, ts, action, result, detail) "
            "VALUES (?,?,?,?,?)",
            (comp_id, dbmod.now_iso(), action, result, detail),
        )

    def open_door(self, comp, reason="intake"):
        """开门（投放预占或取件）。故障门返回失败。

        返回 (ok, detail)。
        """
        if comp["door_status"] == "fault":
            self._log(comp["id"], "open", "fail", "门锁故障，无法弹门")
            return False, "门锁故障，无法弹门"
        if comp["door_status"] == "open":
            # 门已经处于打开状态：不重复分配，但取件场景允许上报关闭
            self._log(comp["id"], "open", "fail", "门已处于打开状态")
            return False, "该格口门未关闭，不可分配"
        self.conn.execute(
            "UPDATE compartment SET door_status='open' WHERE id=?", (comp["id"],)
        )
        self._log(comp["id"], "open", "success", f"弹门成功({reason})")
        return True, "弹门成功"

    def close_door(self, comp, reason="intake"):
        """关门回执：仅当门处于打开状态才有效，确认包裹真实入柜。"""
        if comp["door_status"] == "fault":
            self._log(comp["id"], "close", "fail", "门锁故障，关门回执无效")
            return False, "门锁故障，关门回执无效"
        if comp["door_status"] != "open":
            self._log(comp["id"], "close", "fail", "门未处于打开状态，回执无效")
            return False, "门未处于打开状态，回执无效"
        self.conn.execute(
            "UPDATE compartment SET door_status='closed' WHERE id=?", (comp["id"],)
        )
        self._log(comp["id"], "close", "success", f"关门回执成功({reason})")
        return True, "关门回执成功"

    def report_fault(self, comp_id, note=""):
        """上报柜门故障并留存说明，格口转为维修态。"""
        self.conn.execute(
            "UPDATE compartment SET status='fault', door_status='fault', fault_note=? WHERE id=?",
            (note, comp_id),
        )
        self._log(comp_id, "open", "fail", f"故障上报: {note}")
        return True

    def repair(self, comp_id):
        """维修完成：如果里面没有包裹则恢复空闲。"""
        comp = self.conn.execute(
            "SELECT * FROM compartment WHERE id=?", (comp_id,)
        ).fetchone()
        if comp["occupant_package_id"]:
            return False, "格口内仍有封存包裹，需先取出或转移"
        self.conn.execute(
            "UPDATE compartment SET status='free', door_status='closed', fault_note=NULL WHERE id=?",
            (comp_id,),
        )
        self._log(comp_id, "close", "success", "维修完成，恢复可用")
        return True, "维修完成"

    def force_open_with_approval(self, comp, approver):
        """强制开柜：必须有物业审批（在服务层校验后调用）。"""
        self.conn.execute(
            "UPDATE compartment SET door_status='open' WHERE id=?", (comp["id"],)
        )
        self._log(comp["id"], "force_open", "success", f"物业 {approver} 批准强开")
        return True, "强制开门成功"


class SmsAdapter:
    """模拟短信通道：写入 notification 表，按规则模拟失败。"""

    # 测试用：让指定运单的前 N 次发送失败（验证重试）
    FAIL_FIRST_N = {}

    def __init__(self, conn):
        self.conn = conn

    def queue(self, package_id, phone, text):
        cur = self.conn.execute(
            "INSERT INTO notification(package_id, ts, phone, text, status, attempts) "
            "VALUES (?,?,?,?, 'queued', 0)",
            (package_id, dbmod.now_iso(), phone, text),
        )
        return cur.lastrowid

    def send(self, notif):
        """发送单条通知，返回送达结果。"""
        pkg = self.conn.execute(
            "SELECT waybill FROM package WHERE id=?", (notif["package_id"],)
        ).fetchone()
        wb = pkg["waybill"] if pkg else ""
        fail_n = SmsAdapter.FAIL_FIRST_N.get(wb, 0)
        attempts = notif["attempts"] + 1
        if attempts <= fail_n:
            self.conn.execute(
                "UPDATE notification SET attempts=?, status='failed', last_error=? WHERE id=?",
                (attempts, "模拟通道繁忙(网关500)", notif["id"]),
            )
            return False, "模拟通道繁忙(网关500)"
        receipt = f"RCPT-{notif['id']}-{attempts}-{wb[-4:]}"
        self.conn.execute(
            "UPDATE notification SET attempts=?, status='sent', last_error=NULL, receipt_id=? WHERE id=?",
            (attempts, receipt, notif["id"]),
        )
        return True, receipt

    @classmethod
    def inject_failure(cls, waybill, n=1):
        """让某运单短信前 n 次发送失败。"""
        cls.FAIL_FIRST_N[waybill] = n

    @classmethod
    def reset_failure(cls):
        cls.FAIL_FIRST_N.clear()
