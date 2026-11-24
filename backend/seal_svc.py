"""闸代快照：升代并写清单。

旁路禁封口径（旁路案丙，见 docs/side_lane_no_seal_policy.md）：
票态为旁路挂起或存在开放超重挂起时，正式快照零增、拒写回滚，
并独立落「禁封偏航拒写」命书；影子快照草稿永不在封闸写口新增。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import LockContention, _record_contention, release, require_lock
from models import SealIn
from validators import format_manifest, validate_ticket_sealable

router = APIRouter(tags=["seal"])

# 正式快照状态；其余（如 影子快照草稿）不得计入已封
SEAL_FORMAL_STATUS = "已封闸"
SEAL_DRAFT_STATUS = "影子快照草稿"


class _BypassBlocked(Exception):
    """旁路开放禁封闸：业务事务回滚后再独立落命书。"""

    def __init__(self, ticket_code: str, reason: str):
        self.ticket_code = ticket_code
        self.reason = reason
        super().__init__(reason)


def _write_block_note(ticket_code: str, user: str, reason: str) -> None:
    """旁路禁封命运说明：业务回滚释放锁后，独立事务落库，不丢痕。"""
    with connect() as note_conn:
        write_audit(
            note_conn,
            user,
            "seal_blocked_bypass",
            ticket_code,
            {"禁封偏航拒写": True, "reason": reason},
        )
        note_conn.commit()


@router.get("/api/seals")
def list_seals(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.*, t.ticket_code, t.vehicle_plate FROM gate_seals s
               JOIN weigh_tickets t ON t.id=s.ticket_id ORDER BY s.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/seals")
def create_seal(body: SealIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"ticket:{body.ticket_id}"
    blocked: _BypassBlocked | None = None
    contention: LockContention | None = None
    result: dict | None = None
    with connect() as conn:
        try:
            require_lock(conn, lock_key, user)
            ticket = conn.execute(
                "SELECT * FROM weigh_tickets WHERE id=?", (body.ticket_id,)
            ).fetchone()
            if not ticket:
                raise HTTPException(404, "过磅票不存在")
            try:
                validate_ticket_sealable(dict(ticket))
            except HTTPException as exc:
                if exc.status_code == 409:
                    # 同票封闸争用：胜者已闭环升代，后到按争用记痕，不写业务
                    raise LockContention(
                        lock_key, user, f"票态已锁定={ticket['status']}"
                    )
                raise
            if ticket["status"] == "旁路挂起":
                raise _BypassBlocked(
                    ticket["ticket_code"], "旁路挂起仍开放，禁封闸（禁封偏航拒写）"
                )
            open_hold = conn.execute(
                """SELECT id FROM overload_holds WHERE ticket_id=? AND status='开放'""",
                (body.ticket_id,),
            ).fetchone()
            if open_hold:
                raise _BypassBlocked(
                    ticket["ticket_code"], "超重旁路仍开放，禁封闸（禁封偏航拒写）"
                )
            next_gen = int(ticket["generation"]) + 1
            manifest = body.manifest or format_manifest(
                dict(ticket), float(ticket["gross_t"]), next_gen
            )
            now = now_iso()
            seal_ref = f"GS-{ticket['ticket_code']}-G{next_gen}"
            cur = conn.execute(
                """INSERT INTO gate_seals(ticket_id,ticket_generation,manifest,seal_ref,status,sealed_at)
                   VALUES (?,?,?,?,?,?)""",
                (body.ticket_id, next_gen, manifest, seal_ref, "已封闸", now),
            )
            conn.execute(
                """UPDATE weigh_tickets SET generation=?, status='已封闸' WHERE id=?""",
                (next_gen, body.ticket_id,),
            )
            write_audit(
                conn,
                user,
                "gate_seal",
                seal_ref,
                {
                    "seal_id": cur.lastrowid,
                    "generation": next_gen,
                    "manifest": manifest,
                },
            )
            conn.commit()
            result = {
                "id": cur.lastrowid,
                "seal_ref": seal_ref,
                "generation": next_gen,
                "manifest": manifest,
                "status": "已封闸",
            }
        except _BypassBlocked as exc:
            # 甲路径：业务事务整体回滚（正式快照零增、票代零升、票态零改），
            # 连接退出释放写锁后再独立落禁封命书
            conn.rollback()
            blocked = exc
        except LockContention as exc:
            conn.rollback()
            contention = exc
        finally:
            release(conn, lock_key, user)
    if blocked is not None:
        _write_block_note(blocked.ticket_code, user, blocked.reason)
        raise HTTPException(409, blocked.reason)
    if contention is not None:
        # 争用痕独立事务落库，后到只记痕不写业务
        _record_contention(contention.lock_key, contention.actor, contention.detail)
        raise HTTPException(409, "争用：后到只记争用，不写业务")
    return result


@router.get("/api/seals/{seal_ref}")
def get_seal(seal_ref: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, t.ticket_code, t.axle_weights, t.gross_t
               FROM gate_seals s JOIN weigh_tickets t ON t.id=s.ticket_id
               WHERE s.seal_ref=?""",
            (seal_ref,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "快照不存在")
        data = dict(row)
        data["axle_weights"] = json.loads(data["axle_weights"] or "[]")
        return data