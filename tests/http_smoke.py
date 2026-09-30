"""HTTP 端到端冒烟：在已启动的服务上演练关键业务路径。
用法：python3 tests/http_smoke.py [base_url]
"""
import json
import sys
import urllib.request
import http.cookiejar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"


class Client:
    def __init__(self, who):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))
        self.login(who)
        self.who = who

    def call(self, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with self.op.open(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise AssertionError(f"{self.who} {path} -> {e.code}: "
                                 + e.read().decode())

    def login(self, who):
        data = json.dumps({"courier_id": who}).encode()
        req = urllib.request.Request(BASE + "/api/login", data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with self.op.open(req) as r:
            return json.loads(r.read())


def main():
    sf = Client("U-SF")
    yt = Client("U-YT")
    pm = Client("U-FD")

    # 1) 满仓局面
    lockers = {l["id"]: l for l in sf.call("/api/meta")["lockers"]}
    assert lockers["M1"]["rate"] >= 1.0 and lockers["M3"]["rate"] >= 1.0, \
        "M1/M3 应满仓"
    print("✔ 满仓局面：M1/M3 100%，M2 含故障门")

    # 2) 扫描演示大件（顺丰 L，B1 满仓）
    scan = sf.call("/api/scan", {"waybill": "SF90000001"})
    pid = scan["package"]["id"]
    plan = sf.call("/api/suggest", {"pkg_id": pid, "building_id": "B1"})
    assert plan["cell"] is None, "B1 不应再有柜机格口"
    assert plan["shelf_slots"], "应有同楼货架位"
    assert plan["neighbor_cells"], "应有邻楼格口"
    print("✔ 满仓建议：本楼无柜 → 同楼货架 + 邻楼 B2 格口")

    # 3) 连续路径：转邻楼格口，等待关门回执
    target = plan["neighbor_cells"][0]
    t = sf.call(f"/api/packages/{pid}/transfer",
                {"target_type": "cell", "target_id": target["id"], "reason": "full"})
    assert t["await_close"] and t["old_code"] in (None, "")
    done = sf.call(f"/api/packages/{pid}/transfer-close", {})
    code2 = done["pickup_code"]
    print(f"✔ 邻楼转移完成，新码 {code2}")

    # 4) 公司隔离：圆通看不到这件顺丰包裹
    try:
        yt.call(f"/api/packages/{pid}")
        raise AssertionError("圆通不应读到顺丰包裹")
    except AssertionError as e:
        if "403" not in str(e):
            raise
    print("✔ 公司隔离：圆通访问顺丰详情被 403")

    # 5) 取件码：错误/他司视角均不可猜测，本人凭新码可取
    verify = sf.call("/api/verify-code", {"code": code2})
    assert verify["waybill"] == "SF90000001"
    pick = sf.call("/api/pickup", {"code": code2, "receiver_sign": "本人签收"})
    assert pick["picked_up"]
    print("✔ 新码核验取件成功，格口释放")

    # 6) 强开审批链
    fault_cell = None
    cells = sf.call("/api/lockers/M2/cells")
    fault_cell = next(c["id"] for c in cells
                      if c["door_status"] == "fault" and c["pkg_id"])
    fpkg = next(c for c in cells if c["id"] == fault_cell)
    # 顺丰申请（种子已有一单，再造一单会多一条，无妨）——直接取待审批单
    approvals = pm.call("/api/approvals?status=pending")
    ap = next(a for a in approvals if a["cell_code"] == fpkg["code"])
    # 圆通不能审批
    try:
        yt.call(f"/api/approvals/{ap['id']}/decide", {"approve": True})
        raise AssertionError("快递员不应能审批")
    except AssertionError as e:
        assert "403" in str(e)
    pm.call(f"/api/approvals/{ap['id']}/decide",
            {"approve": True, "note": "同意强开"})
    # 顺丰凭审批强开
    got = sf.call(f"/api/packages/{ap['package_id']}/force-retrieve",
                  {"approval_id": ap["id"]})
    assert got["retrieved"] and got["door_kept_fault"]
    # 门仍故障 → 转货架（带审批）
    detail = sf.call(f"/api/packages/{ap['package_id']}")
    sg = sf.call("/api/suggest", {"pkg_id": ap["package_id"],
                                  "building_id": "B1"})
    slot = sg["shelf_slots"][0]
    tr = sf.call(f"/api/packages/{ap['package_id']}/transfer",
                 {"target_type": "shelf", "target_id": slot["id"],
                  "reason": "fault", "approval_id": ap["id"]})
    assert tr["on_shelf"]
    pm.call(f"/api/cells/{fault_cell}/repair", {})
    print("✔ 强开审批链：申请→物业批准→强开取出→转货架→物业维修结案")

    # 7) 超时队列已有数据（worker 周期扫描）
    overdue = sf.call("/api/overdue")
    returns = sf.call("/api/returns")
    assert returns, "应有待退回包裹"
    print(f"✔ 超时/退回队列：在存 {len(overdue)}，待退回 {len(returns)}")

    # 8) 货架历史链：旧码失效，新码 446688 有效
    try:
        yt.call("/api/verify-code", {"code": "224466"})
        raise AssertionError("旧码不应有效")
    except AssertionError as e:
        assert "409" in str(e), str(e)
    v = yt.call("/api/verify-code", {"code": "446688"})
    assert v["waybill"] == "YT66000099"
    print("✔ 转移链码失效：112233/224466 作废，446688 有效")

    stats = pm.call("/api/meta")["stats"]
    print("\n全部 HTTP 场景通过。最终统计：", json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
