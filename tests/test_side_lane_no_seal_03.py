"""旁路禁封第3号（旁路案丙）：三红失败测。

口径：旁路开放禁封闸（甲路径：封闸正式快照零增并拒写回滚）。
- 失败测甲：旁路仍开放却能出正式快照（含拒写无命书、零增未守）
- 失败测乙：对账把影子快照草稿当已封
- 失败测丙：双会话抢同票封闸无锁、争用痕丢失
失败测链禁止跳过，三红后才准改服务。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

BP_TICKET = "WT-BP-C09"
BP_BL = "BL-2026-0105"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin", password: str = "admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _bp_ticket_id(c: TestClient, h: dict) -> int:
    rows = c.get("/api/tickets", headers=h).json()["items"]
    return next(t["id"] for t in rows if t["ticket_code"] == BP_TICKET)


def _open_hold(c: TestClient, h: dict, ticket_id: int) -> dict:
    holds = c.get("/api/overloads?status=开放", headers=h).json()["items"]
    return next(x for x in holds if x["ticket_id"] == ticket_id)


# ---------------- 失败测甲：旁路开放禁封闸，正式快照零增、拒写留命书 ----------------

def test_jia_open_bypass_rejects_seal_with_zero_growth_and_rollback_note():
    c = TestClient(app)
    h = _auth(c)
    tid = _bp_ticket_id(c, h)
    with connect() as conn:
        holds = conn.execute(
            "SELECT id FROM overload_holds WHERE ticket_id=? AND status='开放'", (tid,)
        ).fetchall()
        assert holds, "种子夹具前提：BP-C09 旁路挂起仍开放"
        seals_before = conn.execute(
            "SELECT COUNT(*) c FROM gate_seals WHERE ticket_id=?", (tid,)
        ).fetchone()["c"]
        gen_before = conn.execute(
            "SELECT generation, status FROM weigh_tickets WHERE id=?", (tid,)
        ).fetchone()
        formal_before = conn.execute(
            "SELECT COUNT(*) c FROM gate_seals WHERE ticket_id=? AND status='已封闸'",
            (tid,),
        ).fetchone()["c"]

    resp = c.post("/api/seals", headers=h, json={"ticket_id": tid})

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "旁路" in detail or "挂起" in detail
    with connect() as conn:
        # 正式快照零增、票代零升、票态零改（拒写回滚）
        formal_after = conn.execute(
            "SELECT COUNT(*) c FROM gate_seals WHERE ticket_id=? AND status='已封闸'",
            (tid,),
        ).fetchone()["c"]
        seals_after = conn.execute(
            "SELECT COUNT(*) c FROM gate_seals WHERE ticket_id=?", (tid,)
        ).fetchone()["c"]
        gen_after = conn.execute(
            "SELECT generation, status FROM weigh_tickets WHERE id=?", (tid,)
        ).fetchone()
        assert formal_after == formal_before
        assert seals_after == seals_before
        assert gen_after["generation"] == gen_before["generation"]
        assert gen_after["status"] == gen_before["status"] == "旁路挂起"
        # 拒写须留命书：禁封偏航拒写，独立落痕不随业务回滚
        note = conn.execute(
            """SELECT id FROM audit_trail
               WHERE action='seal_blocked_bypass' AND target=?
                 AND payload LIKE '%禁封偏航拒写%'""",
            (BP_TICKET,),
        ).fetchone()
        assert note, "旁路开放拒封须写旁路禁封命运说明（禁封偏航拒写）"


# ---------------- 失败测乙：对账不得把影子草稿当已封 ----------------

def test_yi_reconcile_must_not_count_shadow_draft_as_sealed():
    c = TestClient(app)
    h = _auth(c)
    rec = c.get(f"/api/reconcile/{BP_BL}", headers=h).json()
    seals = rec["seals"]
    # 影子草稿存在但不得计入已封
    assert any(s["status"] == "影子快照草稿" for s in seals), "种子前提：存在影子快照草稿"
    assert rec["seal_count"] == 0
    assert rec["formal_seal_count"] == 0
    assert rec["proof_ready"] is False
    assert rec["shadow_draft_count"] == 1
    # 单票证明同样不得把草稿当已封
    proof = c.get(f"/api/proof/ticket/{BP_TICKET}", headers=h).json()
    assert proof["has_seal"] is False
    assert proof["shadow_draft"] is True
    assert proof["overload_closed"] is False
    assert proof["coherent"] is False

    # 闭环旁路后晋升正式：影子不得自动变正式，须经封闸晋升且零改原草稿
    tid = _bp_ticket_id(c, h)
    hold = _open_hold(c, h, tid)
    closed = c.post(
        "/api/overloads/close",
        headers=h,
        json={"hold_id": hold["id"], "note": "驳载完成，旁路闭环"},
    )
    assert closed.status_code == 200
    # 闭环瞬间草稿仍为草稿（禁止自动晋升、禁止改草稿）
    with connect() as conn:
        draft = conn.execute(
            "SELECT status, ticket_generation FROM gate_seals WHERE seal_ref='GS-WT-BP-C09-DRAFT1'"
        ).fetchone()
        assert draft["status"] == "影子快照草稿"
    promoted = c.post("/api/seals", headers=h, json={"ticket_id": tid})
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "已封闸"
    rec2 = c.get(f"/api/reconcile/{BP_BL}", headers=h).json()
    assert rec2["formal_seal_count"] == 1
    assert rec2["shadow_draft_count"] == 1
    assert rec2["seal_count"] == 1
    assert rec2["proof_ready"] is True


# ---------------- 失败测丙：双会话抢同票封闸，占票锁、后到只记争用痕 ----------------

def test_bing_concurrent_seal_requires_ticket_lock_and_keeps_contention_trace():
    c = TestClient(app)
    h1 = _auth(c, "admin", "admin123")
    h2 = _auth(c, "writer", "writer123")
    tid = _bp_ticket_id(c, h1)
    hold = _open_hold(c, h1, tid)
    c.post(
        "/api/overloads/close",
        headers=h1,
        json={"hold_id": hold["id"], "note": "闭环供抢闸"},
    )

    # 先占票锁：后到封闸须 409，且争用痕独立落库（不随业务事务回滚）
    locked = c.post(
        "/api/locks/acquire", headers=h1, json={"lock_key": f"ticket:{tid}"}
    )
    assert locked.status_code == 200
    blocked = c.post("/api/seals", headers=h2, json={"ticket_id": tid})
    assert blocked.status_code == 409
    with connect() as conn:
        trace = conn.execute(
            "SELECT actor, action, detail FROM contention_log WHERE lock_key=?",
            (f"ticket:{tid}",),
        ).fetchall()
        assert trace, "后到会话只记争用痕，痕不得随回滚丢失"
        assert any(r["actor"] == "writer" for r in trace)
        no_business = conn.execute(
            "SELECT COUNT(*) c FROM gate_seals WHERE ticket_id=?", (tid,)
        ).fetchone()["c"]
        # 仅种子草稿一行，后到者零业务写入
        assert no_business == 1
    c.post(
        "/api/locks/release", headers=h1, json={"lock_key": f"ticket:{tid}"}
    )

    # 真并发双会话同票封闸：恰好一成一争，正式快照只许一行
    barrier = threading.Barrier(2)
    outcomes: list[tuple] = []

    def worker(user: str, pw: str) -> None:
        cc = TestClient(app)
        hh = _auth(cc, user, pw)
        barrier.wait()
        rr = cc.post("/api/seals", headers=hh, json={"ticket_id": tid})
        outcomes.append((user, rr.status_code))

    t1 = threading.Thread(target=worker, args=("admin", "admin123"))
    t2 = threading.Thread(target=worker, args=("writer", "writer123"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert sorted(code for _, code in outcomes) == [200, 409], outcomes
    with connect() as conn:
        formal = conn.execute(
            "SELECT COUNT(*) c FROM gate_seals WHERE ticket_id=? AND status='已封闸'",
            (tid,),
        ).fetchone()["c"]
        assert formal == 1
        traces = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=? AND action='争用'",
            (f"ticket:{tid}",),
        ).fetchone()["c"]
        assert traces >= 1
        left_locks = conn.execute(
            "SELECT COUNT(*) c FROM pier_locks WHERE lock_key=?", (f"ticket:{tid}",)
        ).fetchone()["c"]
        assert left_locks == 0
