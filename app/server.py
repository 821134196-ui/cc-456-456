"""HTTP 服务：JSON API + 静态前端。仅用标准库。

会话：登录后下发内存 session cookie；角色分 courier / property，
公司隔离在 Service 层强制。
"""
from __future__ import annotations

import json
import os
import secrets
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .db import DB
from .services import DomainError, Service
from .seed import seed
from .workers import Worker

WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "web")

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class AppState:
    def __init__(self, db_path, evidence_dir, reset=True):
        self.db = DB(db_path)
        self.svc = Service(self.db, evidence_dir)
        if reset:
            # 重置演示数据时清掉上一轮的异常凭证，避免同运单凭证堆积
            for name in os.listdir(evidence_dir):
                fp = os.path.join(evidence_dir, name)
                if os.path.isfile(fp):
                    os.remove(fp)
            seed(self.db, self.svc)
        self.sessions = {}          # token -> courier id
        self.worker = Worker(self.db, self.svc)
        self.worker.start()

    def courier(self, token):
        if not token:
            return None
        cid = self.sessions.get(token)
        if not cid:
            return None
        return self.db.q1("SELECT * FROM couriers WHERE id=?", (cid,))


def make_handler(state: AppState):
    svc = state.svc

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # 静默；需要时可用 --verbose

        # ---------- 基础 ----------
        def _send_json(self, obj, status=200, extra_headers=None):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _cookie_token(self):
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            ck = SimpleCookie()
            ck.load(raw)
            if "sid" in ck:
                return ck["sid"].value
            return None

        def _actor(self, require=True):
            actor = state.courier(self._cookie_token())
            if require and not actor:
                raise DomainError("UNAUTHORIZED", "请先选择登录身份")
            return actor

        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise DomainError("BAD_JSON", "请求体不是合法 JSON")

        def _err(self, e):
            status = {
                "UNAUTHORIZED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404,
            }.get(e.code, 409)
            self._send_json({"error": {"code": e.code, "message": str(e),
                                       "extra": e.extra}}, status)

        # ---------- GET ----------
        def do_GET(self):
            try:
                path = urlparse(self.path).path
                qs = {}
                for kv in urlparse(self.path).query.split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        qs[k] = v

                if path in STATIC:
                    return self._static(path)
                if path == "/api/me":
                    a = state.courier(self._cookie_token())
                    return self._send_json({"actor": dict(a) if a else None})
                if path == "/api/meta":
                    self._actor()
                    return self._send_json({
                        "buildings": [dict(b) for b in svc.buildings()],
                        "lockers": svc.lockers_overview(),
                        "couriers": [dict(c) for c in state.db.q(
                            "SELECT * FROM couriers ORDER BY role,id")],
                        "recipients": [dict(r) for r in state.db.q(
                            "SELECT id,code,name,phone FROM recipients ORDER BY id")],
                        "stats": svc.stats(),
                    })
                if path == "/api/lockers":
                    self._actor()
                    return self._send_json(svc.lockers_overview(qs.get("building")))
                if path.startswith("/api/lockers/") and path.endswith("/cells"):
                    self._actor()
                    lid = path.split("/")[3]
                    return self._send_json(svc.locker_cells(lid))
                if path.startswith("/api/cells/"):
                    self._actor()
                    cid = path.split("/")[3]
                    cell = state.db.q1(
                        "SELECT c.*, l.name locker_name, l.building_id FROM cells c "
                        "JOIN lockers l ON l.id=c.locker_id WHERE c.id=?", (cid,))
                    if not cell:
                        raise DomainError("NOT_FOUND", "格口不存在")
                    return self._send_json(dict(cell))
                if path == "/api/shelves":
                    self._actor()
                    return self._send_json(svc.shelf_grid(qs["building"]))
                if path == "/api/packages":
                    actor = self._actor()
                    return self._send_json(svc.list_packages(
                        actor, qs.get("status"), qs.get("building")))
                if path.startswith("/api/packages/"):
                    actor = self._actor()
                    pid = path.split("/")[3]
                    return self._send_json(svc.package_detail(actor, pid))
                if path == "/api/overdue":
                    self._actor()
                    return self._send_json(svc.overdue_queue())
                if path == "/api/returns":
                    self._actor()
                    return self._send_json(svc.return_queue())
                if path == "/api/approvals":
                    self._actor()
                    return self._send_json(svc.approvals(qs.get("status")))
                if path == "/api/notifications":
                    actor = self._actor()
                    rows = state.db.q(
                        "SELECT n.*,p.waybill,p.company FROM notifications n "
                        "JOIN packages p ON p.id=n.package_id "
                        + ("WHERE p.company=? " if actor["role"] == "courier" else "")
                        + "ORDER BY n.id DESC LIMIT 100",
                        (actor["company"],) if actor["role"] == "courier" else ())
                    return self._send_json([dict(r) for r in rows])
                self._send_json({"error": {"code": "NOT_FOUND", "message": path}}, 404)
            except DomainError as e:
                self._err(e)
            except Exception as e:  # pragma: no cover
                self._send_json({"error": {"code": "SERVER", "message": str(e)}}, 500)

        def _static(self, path):
            fname, ctype = STATIC[path]
            fpath = os.path.join(WEB_DIR, fname)
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---------- POST ----------
        def do_POST(self):
            try:
                path = urlparse(self.path).path.rstrip("/") or "/"
                body = self._read_json()

                if path == "/api/login":
                    cid = body.get("courier_id")
                    c = state.db.q1("SELECT * FROM couriers WHERE id=?", (cid,))
                    if not c:
                        raise DomainError("BAD_LOGIN", "身份不存在")
                    token = secrets.token_hex(16)
                    state.sessions[token] = cid
                    return self._send_json(
                        {"actor": dict(c)},
                        extra_headers=[("Set-Cookie",
                                        f"sid={token}; Path=/; HttpOnly")])
                if path == "/api/logout":
                    return self._send_json(
                        {"ok": True},
                        extra_headers=[("Set-Cookie", "sid=; Path=/; Max-Age=0")])

                actor = self._actor()
                if path == "/api/scan":
                    return self._send_json(svc.scan(actor, body["waybill"]))
                if path == "/api/packages/register":
                    p = svc.register_package(
                        actor, body["waybill"], body["recipient_code"],
                        body["size"], int(body["w"]), int(body["h"]), int(body["d"]))
                    return self._send_json({"package": dict(p)})
                if path == "/api/suggest":
                    return self._send_json(svc.suggest(
                        actor, body["pkg_id"], body["building_id"]))
                if path == "/api/cells/acquire":
                    return self._send_json(svc.acquire_cell(
                        actor, body["pkg_id"], body["cell_id"]))

                # /api/packages/:id/<action>
                if path.startswith("/api/packages/"):
                    parts = path.split("/")
                    pid = parts[3]
                    action = parts[4] if len(parts) > 4 else ""
                    return self._pkg_action(actor, pid, action, body)

                if path == "/api/pickup":
                    return self._send_json(svc.pickup(
                        actor, body["code"], body.get("auth_code"),
                        body.get("proxy_name"), body.get("proxy_phone"),
                        body.get("sign", "")))
                if path == "/api/verify-code":
                    p = state.db.q1(
                        "SELECT * FROM packages WHERE pickup_code=? "
                        "AND status IN ('stored','on_shelf')", (body["code"],))
                    if not p:
                        raise DomainError("BAD_CODE", "取件码错误或已失效")
                    r = state.db.q1("SELECT * FROM recipients WHERE id=?",
                                    (p["recipient_id"],))
                    return self._send_json({"waybill": p["waybill"],
                                            "company": p["company"],
                                            "status": p["status"],
                                            "recipient": r["name"],
                                            "phone_mask": r["phone"][:3] + "****"
                                            + r["phone"][-2:],
                                            "deadline": svc._effective_deadline(p).isoformat(),
                                            "pkg_id": p["id"]})
                if path == "/api/cells/report-fault":
                    return self._send_json(svc.report_fault(
                        actor, body["cell_id"], body.get("note", "")))
                if path.startswith("/api/cells/") and path.endswith("/repair"):
                    cid = path.split("/")[3]
                    return self._send_json(svc.repair_cell(actor, cid))
                if path == "/api/force-open":
                    return self._send_json(svc.apply_force_open(
                        actor, body["cell_id"], body["reason"]))
                if path.startswith("/api/approvals/") and path.endswith("/decide"):
                    aid = int(path.split("/")[3])
                    return self._send_json(svc.decide_force_open(
                        actor, aid, bool(body.get("approve")),
                        body.get("note", "")))
                if path == "/api/sim/sms-fail":
                    svc.sms.fail_next(body["phone"], int(body.get("n", 1)))
                    return self._send_json({"ok": True})
                self._send_json({"error": {"code": "NOT_FOUND", "message": path}}, 404)
            except DomainError as e:
                self._err(e)
            except KeyError as e:
                self._err(DomainError("BAD_PARAM", f"缺少参数：{e}"))
            except Exception as e:  # pragma: no cover
                self._send_json({"error": {"code": "SERVER", "message": str(e)}}, 500)

        def _pkg_action(self, actor, pid, action, body):
            if action == "close":
                return self._send_json(svc.close_receipt(actor, pid))
            if action == "abort":
                return self._send_json(svc.abort_stow(actor, pid))
            if action == "transfer":
                return self._send_json(svc.transfer(
                    actor, pid, (body["target_type"], body["target_id"]),
                    body.get("reason", "manual"), body.get("approval_id")))
            if action == "transfer-close":
                return self._send_json(svc.confirm_transfer_close(actor, pid))
            if action == "extend":
                return self._send_json(svc.extend(actor, pid, int(body["hours"])))
            if action == "authorization":
                return self._send_json(svc.create_authorization(
                    actor, pid, body["proxy_name"], body["proxy_phone"]))
            if action == "return":
                return self._send_json(svc.confirm_return(
                    actor, pid, body["receiver_name"], body.get("sign", ""),
                    body.get("note", "")))
            if action == "reject":
                return self._send_json(svc.reject_return(
                    actor, pid, body["note"]))
            if action == "misdelivery":
                return self._send_json(svc.mark_misdelivery(
                    actor, pid, body.get("correct_building_id"),
                    body.get("note", "")))
            if action == "seal":
                return self._send_json(svc.seal_package(
                    actor, pid, body["reason"], body.get("sign", ""),
                    body.get("note", "")))
            if action == "force-retrieve":
                return self._send_json(svc.force_retrieve(
                    actor, pid, int(body["approval_id"])))
            raise DomainError("NOT_FOUND", f"未知操作 {action}")

    return Handler


def serve(host="127.0.0.1", port=8080, db_path="data/locker.db",
          evidence_dir="data/evidence", reset=True):
    state = AppState(db_path, evidence_dir, reset=reset)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"快递柜系统已启动：http://{host}:{port}")
    print(f"数据库 {db_path}｜异常凭证目录 {os.path.abspath(evidence_dir)}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.worker.stop()
        httpd.server_close()
