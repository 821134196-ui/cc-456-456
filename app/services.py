"""核心业务服务：入柜、满仓转移、人工货架、取件核验、超时、退回、强开审批。

所有方法在调用方提供的 SQLite 连接上执行；写操作在方法内部完成提交。
权限模型：
- courier（普通快递员）：只能操作/查看本公司包裹，可申请强开，不可审批
- property（物业）：强开审批、故障维修、全量查看
- frontdesk（前台）：代取核验、货架操作、全量查看
- guest（收件人）：仅凭取件码取件，无列表权限
"""
import os
import secrets
from datetime import datetime, timedelta

from . import db as dbmod
from .adapters import LockAdapter, SmsAdapter

EVIDENCE_DIR = dbmod.EVIDENCE_DIR

# 各尺寸格口默认存放时限（小时）
STORAGE_LIMIT_HOURS = {"S": 48, "M": 36, "L": 24}
# 超时后多久进入退回队列（小时）
RETURN_GRACE_HOURS = 24
# 收件人可申请的延期上限与每次时长
MAX_EXTENSIONS = 1
EXTENSION_HOURS = 24


class ServiceError(Exception):
    def __init__(self, msg, code=400, extra=None):
        super().__init__(msg)
        self.msg = msg
        self.code = code
        self.extra = extra or {}


def _gen_code(n=6):
    """生成 n 位数字取件码（不使用 random，用 secrets 模拟通道）。"""
    return "".join(str(secrets.randbelow(10)) for _ in range(n))


class LockerService:
    def __init__(self, conn, actor=None):
        self.conn = conn
        self.actor = actor  # courier_user 行
        self.lock = LockAdapter(conn)
        self.sms = SmsAdapter(conn)

    # ---------- 工具 ----------
    def _event(self, package_id, etype, detail, actor=None):
        self.conn.execute(
            "INSERT INTO package_event(package_id, ts, type, actor, detail) VALUES (?,?,?,?,?)",
            (package_id, dbmod.now_iso(), etype, actor or (self.actor["id"] if self.actor else "system"), detail),
        )

    def _row(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def _rows(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def _get_pkg(self, package_id):
        pkg = self._row("SELECT * FROM package WHERE id=?", (package_id,))
        if not pkg:
            raise ServiceError("包裹不存在", 404)
        return pkg

    def _check_company(self, pkg):
        """普通快递员只能访问本公司包裹。"""
        if self.actor and self.actor["role"] == "courier" and pkg["company_id"] != self.actor["company_id"]:
            raise ServiceError("公司隔离：无权查看其他快递公司的包裹详情", 403)

    def _require_role(self, *roles):
        if not self.actor or self.actor["role"] not in roles:
            raise ServiceError("权限不足", 403)

    def _active_code(self, package_id):
        return self._row(
            "SELECT * FROM pickup_code WHERE package_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (package_id,),
        )

    def _void_codes(self, package_id, reason):
        codes = self._rows(
            "SELECT * FROM pickup_code WHERE package_id=? AND status='active'", (package_id,)
        )
        for c in codes:
            self.conn.execute(
                "UPDATE pickup_code SET status='void', voided_at=?, void_reason=? WHERE id=?",
                (dbmod.now_iso(), reason, c["id"]),
            )
        return codes

    def _issue_code(self, package_id, issued_for):
        for _ in range(10):
            code = _gen_code()
            dup = self._row("SELECT 1 FROM pickup_code WHERE code=? AND status='active'", (code,))
            if not dup:
                cur = self.conn.execute(
                    "INSERT INTO pickup_code(package_id, code, status, issued_for, created_at) "
                    "VALUES (?,?,'active',?,?)",
                    (package_id, code, issued_for, dbmod.now_iso()),
                )
                return code, cur.lastrowid
        raise ServiceError("取件码生成冲突，请重试", 500)

    def _notify(self, package_id, text):
        r = self._row("SELECT phone FROM recipient WHERE id=(SELECT recipient_id FROM package WHERE id=?)", (package_id,))
        self.sms.queue(package_id, r["phone"], text)

    def _target_desc(self, kind, target_id):
        if kind == "locker":
            row = self._row(
                "SELECT l.name lname, b.name bname, c.code ccode FROM compartment c "
                "JOIN locker l ON l.id=c.locker_id JOIN building b ON b.id=l.building_id WHERE c.id=?",
                (target_id,),
            )
            return f"{row['bname']}-{row['lname']}-{row['ccode']}" if row else target_id
        if kind == "shelf":
            row = self._row(
                "SELECT s.rack_no, s.level_no, s.pos_no, sh.name shname, b.name bname FROM shelf_slot s "
                "JOIN shelf sh ON sh.id=s.shelf_id JOIN building b ON b.id=sh.building_id WHERE s.id=?",
                (target_id,),
            )
            return f"{row['bname']}-{row['shname']} {row['rack_no']}架{row['level_no']}层{row['pos_no']}位" if row else target_id
        return target_id

    def _write_evidence(self, package_id, kind, note):
        os.makedirs(EVIDENCE_DIR, exist_ok=True)
        pkg = self._get_pkg(package_id)
        fname = f"{kind}_{pkg['waybill']}_{dbmod.now_iso().replace(':','-')}.txt"
        path = os.path.join(EVIDENCE_DIR, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"异常凭证（模拟）\n类型: {kind}\n运单: {pkg['waybill']}\n时间: {dbmod.now_iso()}\n")
            f.write(f"记录人: {self.actor['id'] if self.actor else 'system'}\n说明: {note}\n")
        self.conn.execute(
            "INSERT INTO evidence_file(package_id, kind, ts, path, note) VALUES (?,?,?,?,?)",
            (package_id, kind, dbmod.now_iso(), path, note),
        )
        return path

    # ---------- 扫码 ----------
    def scan_package(self, waybill, locker_id):
        """扫描运单：重复扫码拦截、错投检测、返回建议格口。"""
        pkg = self._row("SELECT * FROM package WHERE waybill=?", (waybill,))
        if not pkg:
            raise ServiceError("运单不存在（虚构数据演示）", 404)
        self._check_company(pkg)
        locker = self._row("SELECT * FROM locker WHERE id=?", (locker_id,))
        if not locker:
            raise ServiceError("柜机不存在", 404)

        # 重复扫码：包裹已不在待入柜状态
        if pkg["status"] != "intake":
            pos = self._position_text(pkg)
            self._event(pkg["id"], "repeat_scan", f"重复扫码，当前状态={pkg['status']}，位置={pos}")
            self.conn.commit()
            raise ServiceError(
                "重复扫码：该包裹已处理", 409,
                extra={"status": pkg["status"], "position": pos, "package_id": pkg["id"]},
            )

        # 错投：柜机所在楼与包裹目标楼不一致
        misdelivered = locker["building_id"] != pkg["building_id"]
        suggest = self.find_compartment(locker_id, pkg["size"])
        result = {
            "package_id": pkg["id"],
            "waybill": pkg["waybill"],
            "size": pkg["size"],
            "target_building_id": pkg["building_id"],
            "misdirected": misdelivered,
            "suggest_compartment_id": suggest["id"] if suggest else None,
            "locker_full": suggest is None,
        }
        self._event(pkg["id"], "scan", f"扫码于柜机 {locker['id']}" + ("，疑似错投" if misdelivered else ""))
        self.conn.commit()
        return result

    def _position_text(self, pkg):
        if pkg["current_compartment_id"]:
            return self._target_desc("locker", pkg["current_compartment_id"])
        if pkg["current_shelf_slot_id"]:
            return self._target_desc("shelf", pkg["current_shelf_slot_id"])
        return "未入库"

    # ---------- 格口查询与入柜 ----------
    def find_compartment(self, locker_id, size):
        """匹配最小可用格口：能装下该尺寸的最小格口；门未关闭的格口不可分配。"""
        return self._row(
            "SELECT * FROM compartment WHERE locker_id=? AND status='free' AND door_status='closed' "
            "AND CASE size WHEN 'S' THEN 1 WHEN 'M' THEN 2 WHEN 'L' THEN 3 END "
            "    >= CASE ? WHEN 'S' THEN 1 WHEN 'M' THEN 2 WHEN 'L' THEN 3 END "
            "ORDER BY CASE size WHEN 'S' THEN 1 WHEN 'M' THEN 2 WHEN 'L' THEN 3 END, seq LIMIT 1",
            (locker_id, size),
        )

    def building_has_free(self, building_id, size):
        return self._row(
            "SELECT c.* FROM compartment c JOIN locker l ON l.id=c.locker_id "
            "WHERE l.building_id=? AND c.status='free' AND c.door_status='closed' "
            "AND CASE c.size WHEN 'S' THEN 1 WHEN 'M' THEN 2 WHEN 'L' THEN 3 END "
            "    >= CASE ? WHEN 'S' THEN 1 WHEN 'M' THEN 2 WHEN 'L' THEN 3 END LIMIT 1",
            (building_id, size),
        )

    def allocate(self, waybill, locker_id, actual_size=None):
        """抢占格口并开门。原子 UPDATE 保证两名快递员争用只有一人成功。"""
        pkg = self._row("SELECT * FROM package WHERE waybill=?", (waybill,))
        if not pkg:
            raise ServiceError("运单不存在", 404)
        self._check_company(pkg)
        if pkg["status"] != "intake":
            raise ServiceError("包裹当前状态不可入柜（可能已入柜或重复扫码）", 409)
        locker = self._row("SELECT * FROM locker WHERE id=?", (locker_id,))
        if not locker:
            raise ServiceError("柜机不存在", 404)
        if locker["building_id"] != pkg["building_id"]:
            raise ServiceError("错投：该包裹不属于本楼栋，请转投正确楼栋", 400,
                               extra={"misdirected": True, "target_building_id": pkg["building_id"]})

        size = actual_size or pkg["size"]
        comp = self.find_compartment(locker_id, size)
        if not comp:
            raise ServiceError("柜机满仓或无适配格口，请转人工货架或邻楼柜机", 503,
                               extra={"full": True, "building_id": locker["building_id"]})

        token = secrets.token_hex(8)
        # 原子抢占：只有 free+closed 才能改为 allocating
        cur = self.conn.execute(
            "UPDATE compartment SET status='allocating', alloc_token=? "
            "WHERE id=? AND status='free' AND door_status='closed'",
            (token, comp["id"]),
        )
        if cur.rowcount == 0:
            self.conn.commit()
            raise ServiceError("格口竞争失败：已被另一名快递员锁定，请重新匹配", 409, extra={"race": True})

        ok, detail = self.lock.open_door(
            self._row("SELECT * FROM compartment WHERE id=?", (comp["id"],)), reason="入柜"
        )
        if not ok:
            # 开门失败：释放抢占
            self.conn.execute(
                "UPDATE compartment SET status='free', alloc_token=NULL WHERE id=?", (comp["id"],)
            )
            self.conn.commit()
            raise ServiceError(f"开门失败：{detail}", 500)

        self.conn.execute(
            "UPDATE package SET current_locker_id=?, current_compartment_id=?, size=?, delivered_by=? WHERE id=?",
            (locker_id, comp["id"], size, self.actor["id"] if self.actor else None, pkg["id"]),
        )
        self._event(pkg["id"], "allocate", f"锁定格口 {comp['code']}({size}) 并开门，等待关门回执，token={token}")
        self.conn.commit()
        return {"package_id": pkg["id"], "compartment_id": comp["id"], "code": comp["code"], "alloc_token": token}

    def door_close_receipt(self, package_id, alloc_token):
        """关门回执成功才算入柜；生成取件码与通知。"""
        pkg = self._get_pkg(package_id)
        self._check_company(pkg)
        comp = self._row("SELECT * FROM compartment WHERE id=?", (pkg["current_compartment_id"],))
        if not comp or comp["status"] != "allocating":
            raise ServiceError("包裹不在待关门状态", 409)
        if comp["alloc_token"] != alloc_token:
            raise ServiceError("分配令牌不匹配", 403)

        ok, detail = self.lock.close_door(comp, reason="入柜")
        if not ok:
            self._event(pkg["id"], "close_fail", detail)
            self.conn.commit()
            raise ServiceError(f"关门回执失败：{detail}，格口保持开门不可分配", 500)

        stored_at = datetime.now()
        deadline = stored_at + timedelta(hours=STORAGE_LIMIT_HOURS[pkg["size"]])
        target = self._target_desc("locker", comp["id"])
        self.conn.execute(
            "UPDATE compartment SET status='occupied', occupant_package_id=?, alloc_token=NULL WHERE id=?",
            (pkg["id"], comp["id"]),
        )
        self.conn.execute(
            "UPDATE package SET status='stored', stored_at=?, deadline_at=? WHERE id=?",
            (stored_at.replace(microsecond=0).isoformat(), deadline.replace(microsecond=0).isoformat(), pkg["id"]),
        )
        code, _ = self._issue_code(pkg["id"], target)
        self._event(pkg["id"], "stored", f"关门回执成功，入柜 {target}，取件码 {code}，时限至 {deadline:%Y-%m-%d %H:%M}")
        self._notify(pkg["id"], f"【商务楼快递柜】您的包裹{pkg['waybill']}已入柜 {target}，取件码 {code}，"
                                f"请于{deadline:%m-%d %H:%M}前取件。旧码如有将自动失效。")
        self.conn.commit()
        return {"ok": True, "compartment_id": comp["id"], "pickup_code": code,
                "deadline_at": deadline.replace(microsecond=0).isoformat()}

    def cancel_intake(self, package_id):
        """开门后放弃投放（如包裹尺寸不符塞不进）：释放格口。"""
        pkg = self._get_pkg(package_id)
        self._check_company(pkg)
        comp = self._row("SELECT * FROM compartment WHERE id=?", (pkg["current_compartment_id"],))
        if not comp or comp["status"] != "allocating":
            raise ServiceError("无待取消的投放", 409)
        self.lock.close_door(comp, reason="取消投放")
        self.conn.execute(
            "UPDATE compartment SET status='free', door_status='closed', alloc_token=NULL, occupant_package_id=NULL WHERE id=?",
            (comp["id"],),
        )
        self.conn.execute(
            "UPDATE package SET current_locker_id=NULL, current_compartment_id=NULL WHERE id=?", (pkg["id"],)
        )
        self._event(pkg["id"], "intake_cancel", f"取消投放，释放格口 {comp['code']}")
        self.conn.commit()
        return {"ok": True}

    def size_mismatch(self, package_id, actual_size):
        """尺寸不符：开门后发现包裹大于格口，关门失败前纠正尺寸并重新匹配。"""
        pkg = self._get_pkg(package_id)
        self._check_company(pkg)
        comp = self._row("SELECT * FROM compartment WHERE id=?", (pkg["current_compartment_id"],))
        if not comp or comp["status"] != "allocating":
            raise ServiceError("仅开门待投放状态可上报尺寸不符", 409)
        if dbmod.SIZE_ORDER[actual_size] <= dbmod.SIZE_ORDER[comp["size"]]:
            raise ServiceError(f"实际尺寸({actual_size})可放入当前格口({comp['size']})，不构成尺寸不符", 400)
        locker_id = comp["locker_id"]
        self._event(pkg["id"], "size_mismatch", f"包裹实际尺寸 {actual_size} 大于格口 {comp['code']}({comp['size']})")
        self.cancel_intake(package_id)
        bigger = self.find_compartment(locker_id, actual_size)
        if not bigger:
            raise ServiceError("同柜无更大格口，请转人工货架或邻楼柜机", 503, extra={"full": True})
        return self.allocate(pkg["waybill"], locker_id, actual_size=actual_size)

    # ---------- 满仓转移 ----------
    def _release_storage(self, pkg, reason):
        """释放包裹当前占用的柜机格口/货架位，返回 (from_kind, from_target, old_code_str)。"""
        from_kind = from_target = None
        if pkg["current_compartment_id"]:
            from_kind, from_target = "locker", pkg["current_compartment_id"]
            comp = self._row("SELECT * FROM compartment WHERE id=?", (from_target,))
            if comp["door_status"] == "open":
                self.lock.close_door(comp, reason="转移")
            self.conn.execute(
                "UPDATE compartment SET status='free', door_status='closed', occupant_package_id=NULL, "
                "alloc_token=NULL WHERE id=?",
                (from_target,),
            )
        elif pkg["current_shelf_slot_id"]:
            from_kind, from_target = "shelf", pkg["current_shelf_slot_id"]
            self.conn.execute(
                "UPDATE shelf_slot SET status='free', occupant_package_id=NULL WHERE id=?", (from_target,)
            )
        else:
            from_kind = "intake"
        old_codes = self._void_codes(pkg["id"], reason)
        old_code = old_codes[0]["code"] if old_codes else None
        return from_kind, from_target, old_code

    def _log_transfer(self, pkg, reason, from_kind, from_target, to_kind, to_target, old_code, new_code):
        self.conn.execute(
            "INSERT INTO transfer_record(package_id, ts, actor, reason, from_kind, from_target, "
            "to_kind, to_target, old_code, new_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pkg["id"], dbmod.now_iso(), self.actor["id"] if self.actor else "system", reason,
             from_kind, from_target, to_kind, to_target, old_code, new_code),
        )

    def transfer_to_shelf(self, package_id, shelf_id, reason="full"):
        """满仓/故障转入同楼人工货架：旧码作废，新码+新通知。"""
        pkg = self._get_pkg(package_id)
        self._check_company(pkg)
        shelf = self._row("SELECT * FROM shelf WHERE id=?", (shelf_id,))
        if not shelf:
            raise ServiceError("货架不存在", 404)
        if pkg["status"] == "intake" and shelf["building_id"] != pkg["building_id"]:
            raise ServiceError("错投：不能跨楼转入货架，请选择正确楼栋", 400)
        if pkg["status"] not in ("intake", "stored", "on_shelf", "overdue"):
            raise ServiceError(f"包裹状态 {pkg['status']} 不可转移", 409)

        slot = self._row(
            "SELECT * FROM shelf_slot WHERE shelf_id=? AND status='free' ORDER BY rack_no, level_no, pos_no LIMIT 1",
            (shelf_id,),
        )
        if not slot:
            raise ServiceError("人工货架已满", 503)

        from_kind, from_target, old_code = self._release_storage(pkg, f"转移货架({reason})")
        self.conn.execute(
            "UPDATE shelf_slot SET status='occupied', occupant_package_id=? WHERE id=?", (pkg["id"], slot["id"])
        )
        target_desc = self._target_desc("shelf", slot["id"])
        code, _ = self._issue_code(pkg["id"], target_desc)
        stored_at = datetime.now() if not pkg["stored_at"] else datetime.fromisoformat(pkg["stored_at"])
        deadline = stored_at + timedelta(hours=STORAGE_LIMIT_HOURS[pkg["size"]])
        self.conn.execute(
            "UPDATE package SET status='on_shelf', current_locker_id=NULL, current_compartment_id=NULL, "
            "current_shelf_slot_id=?, deadline_at=? WHERE id=?",
            (slot["id"], deadline.replace(microsecond=0).isoformat(), pkg["id"]),
        )
        self._log_transfer(pkg, reason, from_kind, from_target, "shelf", slot["id"], old_code, code)
        self._event(pkg["id"], "transfer", f"转入人工货架 {target_desc}，旧码 {old_code} 作废，新码 {code}")
        old_tip = f"原取件码 {old_code} 已作废。" if old_code else ""
        self._notify(pkg["id"], f"【商务楼快递柜】因柜机调整，您的包裹{pkg['waybill']}已改存人工货架 "
                                f"{target_desc}，新取件码 {code}。{old_tip}取件请至前台核验。")
        self.conn.commit()
        return {"ok": True, "shelf_slot_id": slot["id"], "target": target_desc, "old_code": old_code, "new_code": code}

    def transfer_to_locker(self, package_id, locker_id, reason="full"):
        """转入邻楼（或同楼其他）柜机：旧码作废，重新走开门-关门入柜。

        返回 alloc_token，前端需继续调用 door_close_receipt 完成关门回执。
        """
        pkg = self._get_pkg(package_id)
        self._check_company(pkg)
        locker = self._row("SELECT * FROM locker WHERE id=?", (locker_id,))
        if not locker:
            raise ServiceError("目标柜机不存在", 404)
        if pkg["status"] not in ("intake", "stored", "on_shelf", "overdue"):
            raise ServiceError(f"包裹状态 {pkg['status']} 不可转移", 409)
        comp = self.find_compartment(locker_id, pkg["size"])
        if not comp:
            raise ServiceError("目标柜机同样无适配格口", 503)

        token = secrets.token_hex(8)
        cur = self.conn.execute(
            "UPDATE compartment SET status='allocating', alloc_token=? "
            "WHERE id=? AND status='free' AND door_status='closed'",
            (token, comp["id"]),
        )
        if cur.rowcount == 0:
            self.conn.commit()
            raise ServiceError("格口竞争失败，请重试", 409)

        from_kind, from_target, old_code = self._release_storage(pkg, f"转移柜机({reason})")
        ok, detail = self.lock.open_door(
            self._row("SELECT * FROM compartment WHERE id=?", (comp["id"],)), reason="转移入柜"
        )
        if not ok:
            self.conn.execute(
                "UPDATE compartment SET status='free', alloc_token=NULL WHERE id=?", (comp["id"],)
            )
            self.conn.commit()
            raise ServiceError(f"目标柜机开门失败：{detail}", 500)

        self.conn.execute(
            "UPDATE package SET status='intake', current_locker_id=?, current_compartment_id=?, "
            "current_shelf_slot_id=NULL WHERE id=?",
            (locker_id, comp["id"], pkg["id"]),
        )
        self._log_transfer(pkg, reason, from_kind, from_target, "locker", comp["id"], old_code, None)
        self._event(pkg["id"], "transfer", f"转入柜机 {locker['name']} 格口 {comp['code']}（开门待关门），旧码 {old_code} 作废")
        self.conn.commit()
        return {"package_id": pkg["id"], "compartment_id": comp["id"], "alloc_token": token,
                "old_code": old_code, "need_close_receipt": True}

    def neighbor_options(self, building_id, size):
        """邻楼柜机可选目标（neighbor_group 相同）及各自适配格口情况。"""
        out = []
        buildings = self._rows(
            "SELECT b.* FROM building b WHERE b.neighbor_group=(SELECT neighbor_group FROM building WHERE id=?)",
            (building_id,),
        )
        for b in buildings:
            for lk in self._rows("SELECT * FROM locker WHERE building_id=?", (b["id"],)):
                comp = self.find_compartment(lk["id"], size)
                out.append({
                    "locker_id": lk["id"], "locker_name": lk["name"],
                    "building_id": b["id"], "building_name": b["name"],
                    "compartment_id": comp["id"] if comp else None,
                    "available": comp is not None,
                })
        return out

    # ---------- 取件核验 ----------
    def pickup_verify(self, code, phone_last4=None, proxy_code=None, proxy_name=None):
        """核验取件码：本人核验手机尾号；前台代取必须核对授权码与被授权人。"""
        pc = self._row("SELECT * FROM pickup_code WHERE code=? ORDER BY id DESC LIMIT 1", (code,))
        if not pc:
            raise ServiceError("取件码不存在", 404)
        if pc["status"] == "void":
            self._event(pc["package_id"], "code_invalid", f"使用已作废取件码 {code}（{pc['void_reason']}）")
            self.conn.commit()
            raise ServiceError(f"取件码已失效（{pc['void_reason'] or '已作废'}），请使用最新通知中的新码", 410,
                               extra={"void": True})
        if pc["status"] == "used":
            raise ServiceError("取件码已被使用", 410, extra={"used": True})

        pkg = self._get_pkg(pc["package_id"])
        if pkg["status"] in ("picked", "returned"):
            raise ServiceError("包裹已结束存放", 410)
        recipient = self._row("SELECT * FROM recipient WHERE id=?", (pkg["recipient_id"],))

        mode = "self"
        auth = None
        if proxy_code:
            # 前台代取：核对授权
            self._require_role("frontdesk", "property", "courier")
            auth = self._row(
                "SELECT * FROM proxy_authorization WHERE package_id=? AND auth_code=? AND status='valid'",
                (pkg["id"], proxy_code),
            )
            if not auth:
                self._event(pkg["id"], "proxy_denied", f"代取授权码核验失败：{proxy_code}")
                self.conn.commit()
                raise ServiceError("前台代取授权无效或已使用，必须核对有效授权", 403, extra={"proxy_invalid": True})
            if proxy_name and auth["authorized_name"] != proxy_name:
                raise ServiceError(f"被授权人姓名不符（授权给：{auth['authorized_name']}）", 403)
            mode = "proxy"
        else:
            # 本人取件：手机尾号核对
            if phone_last4 and recipient["phone"][-4:] != phone_last4:
                self._event(pkg["id"], "verify_fail", "本人取件手机尾号不符")
                self.conn.commit()
                raise ServiceError("手机尾号与收件人信息不符", 403)

        # 开门
        if pkg["current_compartment_id"]:
            comp = self._row("SELECT * FROM compartment WHERE id=?", (pkg["current_compartment_id"],))
            if comp["status"] == "fault":
                raise ServiceError("该格口柜门故障已封存，需物业审批强开", 423, extra={"fault": True})
            ok, detail = self.lock.open_door(comp, reason="取件")
            if not ok:
                raise ServiceError(f"开门失败：{detail}", 500)
        self._event(pkg["id"], "pickup_verify", f"核验通过({mode})，开门待取件", actor=(self.actor["id"] if self.actor else "guest"))
        self.conn.commit()
        return {
            "ok": True, "package_id": pkg["id"], "mode": mode,
            "position": self._position_text(pkg),
            "recipient_alias": recipient["alias"],
            "proxy_auth_id": auth["id"] if auth else None,
            "on_shelf": pkg["current_shelf_slot_id"] is not None,
        }

    def pickup_complete(self, package_id, proxy_auth_id=None):
        """关门回执后完成取件，释放位置，码置为 used。"""
        pkg = self._get_pkg(package_id)
        if pkg["current_compartment_id"]:
            comp = self._row("SELECT * FROM compartment WHERE id=?", (pkg["current_compartment_id"],))
            ok, detail = self.lock.close_door(comp, reason="取件")
            if not ok:
                raise ServiceError(f"关门回执失败：{detail}", 500)
            # 故障格口取出后仍保持维修态，须物业显式维修
            self.conn.execute(
                "UPDATE compartment SET occupant_package_id=NULL, alloc_token=NULL, "
                "status=CASE WHEN status='fault' THEN 'fault' ELSE 'free' END, "
                "door_status=CASE WHEN door_status='fault' THEN 'fault' ELSE 'closed' END WHERE id=?",
                (comp["id"],),
            )
        if pkg["current_shelf_slot_id"]:
            self.conn.execute(
                "UPDATE shelf_slot SET status='free', occupant_package_id=NULL WHERE id=?",
                (pkg["current_shelf_slot_id"],),
            )
        self.conn.execute(
            "UPDATE package SET status='picked', picked_at=?, picked_by=?, current_locker_id=NULL, "
            "current_compartment_id=NULL, current_shelf_slot_id=NULL WHERE id=?",
            (dbmod.now_iso(), self.actor["id"] if self.actor else "guest", pkg["id"]),
        )
        self.conn.execute(
            "UPDATE pickup_code SET status='used', voided_at=?, void_reason='取件完成' "
            "WHERE package_id=? AND status='active'",
            (dbmod.now_iso(), pkg["id"]),
        )
        if proxy_auth_id:
            self.conn.execute(
                "UPDATE proxy_authorization SET status='used', used_at=? WHERE id=?",
                (dbmod.now_iso(), proxy_auth_id),
            )
        self._event(pkg["id"], "picked", "取件完成，占用结束")
        self.conn.commit()
        return {"ok": True}

    def create_proxy_authorization(self, package_id, authorized_name):
        """收件人/前台发起代取授权。"""
        pkg = self._get_pkg(package_id)
        if self.actor and self.actor["role"] == "courier":
            self._check_company(pkg)
        code = _gen_code(4)
        self.conn.execute(
            "INSERT INTO proxy_authorization(package_id, auth_code, authorized_name, ts, status) "
            "VALUES (?,?,?,?,'valid')",
            (pkg["id"], code, authorized_name, dbmod.now_iso()),
        )
        self._event(pkg["id"], "proxy_grant", f"生成代取授权：{authorized_name} / {code}")
        self.conn.commit()
        return {"auth_code": code, "authorized_name": authorized_name}

    # ---------- 延期、超时、退回 ----------
    def request_extension(self, package_id, hours=EXTENSION_HOURS):
        """收件人申请延期，次数受限；延期后重置 deadline。"""
        pkg = self._get_pkg(package_id)
        if pkg["ext_count"] >= MAX_EXTENSIONS:
            raise ServiceError(f"每单最多延期 {MAX_EXTENSIONS} 次", 409)
        if pkg["status"] not in ("stored", "on_shelf", "overdue"):
            raise ServiceError("当前状态不可延期", 409)
        base = datetime.fromisoformat(pkg["deadline_at"])
        new_deadline = max(base, datetime.now()) + timedelta(hours=hours)
        self.conn.execute(
            "UPDATE package SET deadline_at=?, ext_count=ext_count+1, "
            "status=CASE WHEN status='overdue' THEN "
            f"  CASE WHEN current_shelf_slot_id IS NOT NULL THEN 'on_shelf' ELSE 'stored' END "
            "ELSE status END WHERE id=?",
            (new_deadline.replace(microsecond=0).isoformat(), pkg["id"]),
        )
        self._event(pkg["id"], "extension", f"收件人延期 {hours} 小时，新时限 {new_deadline:%Y-%m-%d %H:%M}")
        self._notify(pkg["id"], f"【商务楼快递柜】您的包裹{pkg['waybill']}取件时限已延长至 {new_deadline:%m-%d %H:%M}。")
        self.conn.commit()
        return {"ok": True, "deadline_at": new_deadline.isoformat()}

    def return_queue(self):
        """退回队列：return_handoff 中 pending/rejected 的单。"""
        return self._rows(
            "SELECT rh.*, p.waybill, p.size, p.status pkg_status, c.name company_name, "
            "r.alias recipient_alias, r.phone "
            "FROM return_handoff rh JOIN package p ON p.id=rh.package_id "
            "JOIN company c ON c.id=p.company_id JOIN recipient r ON r.id=p.recipient_id "
            "WHERE rh.status IN ('pending','rejected') ORDER BY rh.ts"
        )

    def handover_return(self, handoff_id, handed_to):
        """与快递公司交接退回，签收后包裹 returned，释放占用。"""
        rh = self._row("SELECT * FROM return_handoff WHERE id=?", (handoff_id,))
        if not rh:
            raise ServiceError("退回记录不存在", 404)
        if rh["status"] != "pending":
            raise ServiceError("该退回单不在待交接状态", 409)
        pkg = self._get_pkg(rh["package_id"])
        # 快递员只能接本公司的退回件
        if self.actor and self.actor["role"] == "courier" and pkg["company_id"] != self.actor["company_id"]:
            raise ServiceError("公司隔离：不能签收其他公司的退回件", 403)

        if pkg["current_compartment_id"]:
            comp = self._row("SELECT * FROM compartment WHERE id=?", (pkg["current_compartment_id"],))
            if comp["status"] == "fault":
                raise ServiceError("包裹封存在故障格口，须先强开取出再交接", 423)
            if comp["door_status"] == "open":
                self.lock.close_door(comp, reason="退回取出")
            self.conn.execute(
                "UPDATE compartment SET status='free', door_status='closed', occupant_package_id=NULL WHERE id=?",
                (comp["id"],),
            )
        if pkg["current_shelf_slot_id"]:
            self.conn.execute(
                "UPDATE shelf_slot SET status='free', occupant_package_id=NULL WHERE id=?",
                (pkg["current_shelf_slot_id"],),
            )
        self._void_codes(pkg["id"], "退回交接")
        self.conn.execute(
            "UPDATE package SET status='returned', current_locker_id=NULL, current_compartment_id=NULL, "
            "current_shelf_slot_id=NULL WHERE id=?",
            (pkg["id"],),
        )
        self.conn.execute(
            "UPDATE return_handoff SET status='handed', handed_to=?, handed_at=?, actor=? WHERE id=?",
            (handed_to, dbmod.now_iso(), self.actor["id"] if self.actor else None, rh["id"]),
        )
        self._event(pkg["id"], "return_handed", f"退回交接签收：{handed_to}")
        self.conn.commit()
        return {"ok": True}

    def reject_return(self, handoff_id, reject_reason):
        """退回拒收：封存到异常封存架，写凭证，等待后续处理。"""
        rh = self._row("SELECT * FROM return_handoff WHERE id=?", (handoff_id,))
        if not rh or rh["status"] != "pending":
            raise ServiceError("退回记录不存在或不可拒收", 404)
        pkg = self._get_pkg(rh["package_id"])
        # 释放原位，转入封存架（rack_no=Q）
        if pkg["current_compartment_id"]:
            self.conn.execute(
                "UPDATE compartment SET status='free', door_status='closed', occupant_package_id=NULL WHERE id=?",
                (pkg["current_compartment_id"],),
            )
        if pkg["current_shelf_slot_id"]:
            self.conn.execute(
                "UPDATE shelf_slot SET status='free', occupant_package_id=NULL WHERE id=?",
                (pkg["current_shelf_slot_id"],),
            )
        slot = self._row(
            "SELECT ss.* FROM shelf_slot ss JOIN shelf sh ON sh.id=ss.shelf_id "
            "WHERE sh.building_id=? AND ss.rack_no='Q' AND ss.status='free' ORDER BY ss.id LIMIT 1",
            (pkg["building_id"],),
        )
        if slot:
            self.conn.execute(
                "UPDATE shelf_slot SET status='occupied', occupant_package_id=? WHERE id=?",
                (pkg["id"], slot["id"]),
            )
            slot_id = slot["id"]
        else:
            slot_id = None
        self._void_codes(pkg["id"], "退回拒收封存")
        self.conn.execute(
            "UPDATE package SET status='sealed', current_locker_id=NULL, current_compartment_id=NULL, "
            "current_shelf_slot_id=? WHERE id=?",
            (slot_id, pkg["id"]),
        )
        self.conn.execute(
            "UPDATE return_handoff SET status='rejected', reject_reason=?, actor=? WHERE id=?",
            (reject_reason, self.actor["id"] if self.actor else None, rh["id"]),
        )
        path = self._write_evidence(pkg["id"], "reject", f"退回拒收：{reject_reason}")
        self._event(pkg["id"], "return_rejected", f"退回拒收并封存：{reject_reason}；凭证 {path}")
        self.conn.commit()
        return {"ok": True, "evidence": path, "slot_id": slot_id}

    # ---------- 故障、错投、异常封存、强开 ----------
    def report_compartment_fault(self, compartment_id, note):
        """上报柜门故障：格口维修态；若内有包裹则异常封存，等待强开审批。"""
        self._require_role("courier", "property", "frontdesk")
        comp = self._row("SELECT * FROM compartment WHERE id=?", (compartment_id,))
        if not comp:
            raise ServiceError("格口不存在", 404)
        self.lock.report_fault(compartment_id, note)
        pkg_id = comp["occupant_package_id"]
        if pkg_id:
            pkg = self._get_pkg(pkg_id)
            self._check_company(pkg)
            self._void_codes(pkg_id, "柜门故障封存")
            self.conn.execute("UPDATE package SET status='sealed' WHERE id=?", (pkg_id,))
            path = self._write_evidence(pkg_id, "fault", f"柜门故障：{note}（格口 {comp['code']}）")
            self._event(pkg_id, "sealed", f"柜门故障异常封存，原取件码作废；凭证 {path}")
        self.conn.commit()
        return {"ok": True, "sealed_package": pkg_id}

    def repair_compartment(self, compartment_id):
        self._require_role("property")
        ok, msg = self.lock.repair(compartment_id)
        if not ok:
            raise ServiceError(msg, 409)
        self.conn.commit()
        return {"ok": True}

    def mark_misdirected(self, package_id, note):
        """登记错投凭证，返回正确楼栋以便转投。"""
        pkg = self._get_pkg(package_id)
        self._check_company(pkg)
        path = self._write_evidence(pkg["id"], "misdelivery", note or "扫描发现包裹错投楼栋")
        self._event(pkg["id"], "misdirected", f"错投登记：{note}；凭证 {path}")
        self.conn.commit()
        b = self._row("SELECT * FROM building WHERE id=?", (pkg["building_id"],))
        return {"ok": True, "evidence": path, "target_building": dict(b) if b else None}

    def force_open_apply(self, compartment_id, reason):
        """快递员申请强制开柜。"""
        self._require_role("courier", "frontdesk")
        comp = self._row("SELECT * FROM compartment WHERE id=?", (compartment_id,))
        if not comp:
            raise ServiceError("格口不存在", 404)
        pkg_id = comp["occupant_package_id"]
        if pkg_id:
            self._check_company(self._get_pkg(pkg_id))
        cur = self.conn.execute(
            "INSERT INTO force_open_request(package_id, compartment_id, ts, applicant, reason, status) "
            "VALUES (?,?,?,?,?,'pending')",
            (pkg_id, compartment_id, dbmod.now_iso(), self.actor["id"], reason),
        )
        if pkg_id:
            self._event(pkg_id, "force_apply", f"申请强开 {comp['code']}：{reason}")
        self.conn.commit()
        return {"ok": True, "request_id": cur.lastrowid}

    def force_open_decide(self, request_id, approve, note=""):
        """物业审批。普通快递员无权审批。"""
        self._require_role("property")
        req = self._row("SELECT * FROM force_open_request WHERE id=?", (request_id,))
        if not req:
            raise ServiceError("审批单不存在", 404)
        if req["status"] != "pending":
            raise ServiceError("该审批单已处理", 409)
        status = "approved" if approve else "rejected"
        self.conn.execute(
            "UPDATE force_open_request SET status=?, approver=?, decided_at=?, note=? WHERE id=?",
            (status, self.actor["id"], dbmod.now_iso(), note, req["id"]),
        )
        if req["package_id"]:
            self._event(req["package_id"], "force_decide", f"物业{'批准' if approve else '驳回'}强开：{note}")
        self.conn.commit()
        return {"ok": True, "status": status}

    def force_open_execute(self, request_id):
        """凭已批准的审批单执行强开；开门后取件仍需关门回执结束占用。"""
        req = self._row("SELECT * FROM force_open_request WHERE id=?", (request_id,))
        if not req:
            raise ServiceError("审批单不存在", 404)
        if req["status"] == "pending":
            raise ServiceError("强制开柜必须先经物业审批", 403)
        if req["status"] == "rejected":
            raise ServiceError("审批已被物业驳回，禁止强开", 403)
        if req["status"] == "executed":
            raise ServiceError("该审批单已执行", 409)
        comp = self._row("SELECT * FROM compartment WHERE id=?", (req["compartment_id"],))
        self.lock.force_open_with_approval(comp, req["approver"])
        # 故障门强开后，门先恢复为可操作状态；格口保持占用直到取走
        self.conn.execute(
            "UPDATE compartment SET door_status='open' WHERE id=?", (comp["id"],)
        )
        self.conn.execute(
            "UPDATE force_open_request SET status='executed', executed_at=? WHERE id=?",
            (dbmod.now_iso(), req["id"]),
        )
        if req["package_id"]:
            pkg = self._get_pkg(req["package_id"])
            self._void_codes(req["package_id"], "强制开柜")
            self._event(req["package_id"], "force_executed", f"凭物业审批强开 {comp['code']}，原取件码作废")
        self.conn.commit()
        return {"ok": True, "package_id": req["package_id"]}

    # ---------- 查询视图 ----------
    def locker_view(self, locker_id):
        lk = self._row(
            "SELECT l.*, b.name building_name FROM locker l JOIN building b ON b.id=l.building_id WHERE l.id=?",
            (locker_id,),
        )
        if not lk:
            raise ServiceError("柜机不存在", 404)
        comps = self._rows("SELECT * FROM compartment WHERE locker_id=? ORDER BY seq", (locker_id,))
        total = len(comps)
        occupied = sum(1 for c in comps if c["status"] in ("occupied", "allocating"))
        return {"locker": dict(lk), "compartments": [dict(c) for c in comps],
                "total": total, "occupied": occupied,
                "full_rate": round(occupied / total, 4) if total else 0}

    def shelf_view(self, shelf_id):
        sh = self._row(
            "SELECT s.*, b.name building_name FROM shelf s JOIN building b ON b.id=s.building_id WHERE s.id=?",
            (shelf_id,),
        )
        slots = self._rows("SELECT * FROM shelf_slot WHERE shelf_id=? ORDER BY rack_no, level_no, pos_no", (shelf_id,))
        return {"shelf": dict(sh) if sh else None, "slots": [dict(x) for x in slots]}

    def package_detail(self, package_id):
        pkg = self._get_pkg(package_id)
        self._check_company(pkg)
        events = self._rows("SELECT * FROM package_event WHERE package_id=? ORDER BY id", (package_id,))
        transfers = self._rows("SELECT * FROM transfer_record WHERE package_id=? ORDER BY id", (package_id,))
        codes = self._rows("SELECT id, code, status, issued_for, created_at, voided_at, void_reason "
                           "FROM pickup_code WHERE package_id=? ORDER BY id", (package_id,))
        notifs = self._rows("SELECT id, ts, phone, text, status, attempts, last_error, receipt_id "
                            "FROM notification WHERE package_id=? ORDER BY id", (package_id,))
        comp = self._row("SELECT * FROM compartment WHERE occupant_package_id=?", (package_id,))
        return {"package": dict(pkg), "events": [dict(x) for x in events],
                "transfers": [dict(x) for x in transfers], "codes": [dict(x) for x in codes],
                "notifications": [dict(x) for x in notifs],
                "position": self._position_text(pkg)}

    def list_packages(self, status=None):
        """包裹列表（执行公司隔离）。快递员只返回本公司且详情受限在 server 层处理。"""
        sql = ("SELECT p.*, c.name company_name, r.alias recipient_alias, r.phone, "
               "b.name building_name FROM package p JOIN company c ON c.id=p.company_id "
               "JOIN recipient r ON r.id=p.recipient_id JOIN building b ON b.id=p.building_id")
        args = []
        if self.actor and self.actor["role"] == "courier":
            sql += " WHERE p.company_id=?"
            args.append(self.actor["company_id"])
        if status:
            sql += (" WHERE " if "WHERE" not in sql else " AND ") + "p.status=?"
            args.append(status)
        sql += " ORDER BY p.created_at DESC"
        return [dict(x) for x in self._rows(sql, args)]
