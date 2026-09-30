"""HTTP 服务：静态前端 + JSON API（标准库实现，无第三方依赖）。

鉴权：通过请求头 X-User-Id 或 ?user_id 指定当前操作人（演示用）。
启动：python -m app.server  （默认 0.0.0.0:8000，自动初始化库+种子+调度器）
"""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import db as dbmod
from .services import LockerService, ServiceError
from .scheduler import Scheduler
from . import seed as seedmod

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

_lock = threading.Lock()  # 串行化写请求，放大并发竞争时仍由 DB 原子更新保证正确性


def _service(conn, user_id):
    actor = None
    if user_id:
        actor = conn.execute("SELECT * FROM courier_user WHERE id=?", (user_id,)).fetchone()
        if not actor:
            raise ServiceError("用户不存在", 401)
    return LockerService(conn, actor)


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartLocker/1.0"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} - {fmt % args}")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_err(self, err: ServiceError):
        self._send_json({"error": err.msg, **err.extra}, err.code)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ServiceError("请求体不是合法 JSON", 400)

    def _uid(self, qs):
        return self.headers.get("X-User-Id") or (qs.get("user_id", [None])[0])

    def _serve_static(self, rel):
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            path = os.path.join(WEB_DIR, "index.html")
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(
            os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if not path.startswith("/api/"):
            return self._serve_static(path.lstrip("/") or "index.html")
        try:
            conn = dbmod.get_conn()
            svc = _service(conn, self._uid(qs))
            self._route_get(svc, conn, path, qs)
        except ServiceError as e:
            self._send_err(e)
        except Exception as e:
            self._send_json({"error": f"服务器错误: {e}"}, 500)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def do_POST(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if not path.startswith("/api/"):
            return self._send_json({"error": "not found"}, 404)
        write = path != "/api/scheduler/tick"
        if write:
            _lock.acquire()
        try:
            body = self._read_json()
            conn = dbmod.get_conn()
            try:
                svc = _service(conn, self._uid(qs))
                self._route_post(svc, conn, path, body)
            finally:
                conn.close()
        except ServiceError as e:
            self._send_err(e)
        except Exception as e:
            self._send_json({"error": f"服务器错误: {e}"}, 500)
        finally:
            if write:
                _lock.release()

    # ---------- GET ----------
    def _route_get(self, svc, conn, path, qs):
        m = re.fullmatch(r"/api/meta", path)
        if m:
            return self._send_json({
                "users": [dict(r) for r in conn.execute("SELECT u.*, c.name company_name FROM courier_user u "
                                                        "LEFT JOIN company c ON c.id=u.company_id ORDER BY u.id")],
                "buildings": [dict(r) for r in conn.execute("SELECT * FROM building ORDER BY id")],
                "lockers": [dict(r) for r in conn.execute(
                    "SELECT l.*, b.name building_name FROM locker l JOIN building b ON b.id=l.building_id ORDER BY l.id")],
                "shelves": [dict(r) for r in conn.execute(
                    "SELECT s.*, b.name building_name FROM shelf s JOIN building b ON b.id=s.building_id ORDER BY s.id")],
                "companies": [dict(r) for r in conn.execute("SELECT * FROM company ORDER BY id")],
            })

        m = re.fullmatch(r"/api/lockers/([^/]+)", path)
        if m:
            return self._send_json(svc.locker_view(m.group(1)))

        m = re.fullmatch(r"/api/shelves/([^/]+)", path)
        if m:
            return self._send_json(svc.shelf_view(m.group(1)))

        m = re.fullmatch(r"/api/packages", path)
        if m:
            return self._send_json({"items": svc.list_packages(qs.get("status", [None])[0])})

        m = re.fullmatch(r"/api/packages/([^/]+)", path)
        if m:
            return self._send_json(svc.package_detail(m.group(1)))

        m = re.fullmatch(r"/api/buildings/([^/]+)/neighbors", path)
        if m:
            size = qs.get("size", ["S"])[0]
            return self._send_json({"options": svc.neighbor_options(m.group(1), size)})

        m = re.fullmatch(r"/api/overdue", path)
        if m:
            return self._send_json({"items": svc.list_packages("overdue")})

        m = re.fullmatch(r"/api/returns", path)
        if m:
            return self._send_json({"items": [dict(r) for r in svc.return_queue()]})

        m = re.fullmatch(r"/api/force-open", path)
        if m:
            rows = conn.execute(
                "SELECT f.*, c.code comp_code, l.name locker_name FROM force_open_request f "
                "JOIN compartment c ON c.id=f.compartment_id JOIN locker l ON l.id=c.locker_id "
                "ORDER BY CASE f.status WHEN 'pending' THEN 0 ELSE 1 END, f.id DESC").fetchall()
            return self._send_json({"items": [dict(r) for r in rows]})

        m = re.fullmatch(r"/api/dashboard", path)
        if m:
            return self._send_json(self._dashboard(conn))

        m = re.fullmatch(r"/api/scheduler/tick", path)
        if m:
            return self._send_json({"note": "请用 POST 触发"})

        self._send_json({"error": "not found"}, 404)

    def _dashboard(self, conn):
        lockers = []
        for lk in conn.execute(
                "SELECT l.*, b.name building_name FROM locker l JOIN building b ON b.id=l.building_id").fetchall():
            view = LockerService(conn, None).locker_view(lk["id"])
            lockers.append({k: view[k] for k in ("total", "occupied", "full_rate")} | {
                "id": lk["id"], "name": lk["name"], "building_name": lk["building_name"]})
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM package GROUP BY status").fetchall()}
        counts["return_pending"] = conn.execute(
            "SELECT COUNT(*) n FROM return_handoff WHERE status='pending'").fetchone()["n"]
        counts["force_pending"] = conn.execute(
            "SELECT COUNT(*) n FROM force_open_request WHERE status='pending'").fetchone()["n"]
        counts["notify_pending"] = conn.execute(
            "SELECT COUNT(*) n FROM notification WHERE status IN ('queued','failed')").fetchone()["n"]
        return {"lockers": lockers, "counts": counts}

    # ---------- POST ----------
    def _route_post(self, svc, conn, path, b):
        def need(k):
            if k not in b:
                raise ServiceError(f"缺少参数: {k}", 400)
            return b[k]

        m = re.fullmatch(r"/api/packages/scan", path)
        if m:
            return self._send_json(svc.scan_package(need("waybill"), need("locker_id")))

        m = re.fullmatch(r"/api/packages/allocate", path)
        if m:
            return self._send_json(svc.allocate(need("waybill"), need("locker_id"), b.get("actual_size")))

        m = re.fullmatch(r"/api/packages/([^/]+)/close-receipt", path)
        if m:
            return self._send_json(svc.door_close_receipt(m.group(1), need("alloc_token")))

        m = re.fullmatch(r"/api/packages/([^/]+)/cancel", path)
        if m:
            return self._send_json(svc.cancel_intake(m.group(1)))

        m = re.fullmatch(r"/api/packages/([^/]+)/size-mismatch", path)
        if m:
            return self._send_json(svc.size_mismatch(m.group(1), need("actual_size")))

        m = re.fullmatch(r"/api/packages/([^/]+)/transfer-shelf", path)
        if m:
            return self._send_json(svc.transfer_to_shelf(m.group(1), need("shelf_id"), b.get("reason", "full")))

        m = re.fullmatch(r"/api/packages/([^/]+)/transfer-locker", path)
        if m:
            return self._send_json(svc.transfer_to_locker(m.group(1), need("locker_id"), b.get("reason", "full")))

        m = re.fullmatch(r"/api/pickup/verify", path)
        if m:
            return self._send_json(svc.pickup_verify(
                need("code"), b.get("phone_last4"), b.get("proxy_code"), b.get("proxy_name")))

        m = re.fullmatch(r"/api/packages/([^/]+)/pickup-complete", path)
        if m:
            return self._send_json(svc.pickup_complete(m.group(1), b.get("proxy_auth_id")))

        m = re.fullmatch(r"/api/packages/([^/]+)/proxy", path)
        if m:
            return self._send_json(svc.create_proxy_authorization(m.group(1), need("authorized_name")))

        m = re.fullmatch(r"/api/packages/([^/]+)/extend", path)
        if m:
            return self._send_json(svc.request_extension(m.group(1), int(b.get("hours", 24))))

        m = re.fullmatch(r"/api/packages/([^/]+)/misdirect", path)
        if m:
            return self._send_json(svc.mark_misdirected(m.group(1), b.get("note", "")))

        m = re.fullmatch(r"/api/returns/([^/]+)/handover", path)
        if m:
            return self._send_json(svc.handover_return(int(m.group(1)), need("handed_to")))

        m = re.fullmatch(r"/api/returns/([^/]+)/reject", path)
        if m:
            return self._send_json(svc.reject_return(int(m.group(1)), need("reject_reason")))

        m = re.fullmatch(r"/api/compartments/([^/]+)/fault", path)
        if m:
            return self._send_json(svc.report_compartment_fault(m.group(1), need("note")))

        m = re.fullmatch(r"/api/compartments/([^/]+)/repair", path)
        if m:
            return self._send_json(svc.repair_compartment(m.group(1)))

        m = re.fullmatch(r"/api/compartments/([^/]+)/force-open/apply", path)
        if m:
            return self._send_json(svc.force_open_apply(m.group(1), need("reason")))

        m = re.fullmatch(r"/api/force-open/(\d+)/decide", path)
        if m:
            return self._send_json(svc.force_open_decide(int(m.group(1)), bool(need("approve")), b.get("note", "")))

        m = re.fullmatch(r"/api/force-open/(\d+)/execute", path)
        if m:
            return self._send_json(svc.force_open_execute(int(m.group(1))))

        m = re.fullmatch(r"/api/scheduler/tick", path)
        if m:
            from .scheduler import Scheduler
            return self._send_json({"stats": Scheduler(run_immediately=False).tick_once()})

        self._send_json({"error": "not found"}, 404)


def serve(host="0.0.0.0", port=8000, prepare=True):
    dbmod.init_db()
    if prepare:
        seedmod.main(reset_data=os.environ.get("SMART_LOCKER_RESET", "1") == "1")
    sched = Scheduler()
    sched.start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"商务楼快递柜系统已启动: http://{host if host != '0.0.0.0' else 'localhost'}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭...")
    finally:
        sched.stop()
        httpd.server_close()


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    serve(port=p)
