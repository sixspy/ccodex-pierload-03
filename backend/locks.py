"""码头锁与争用登记（写入路径嵌入）。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def _record_contention(lock_key: str, actor: str, detail: str) -> None:
    """争用痕独立事务落库。

    调用方须在业务事务回滚、写锁释放后调用，避免 SQLite 嵌套写互锁；
    痕不随业务事务回滚而丢失。
    """
    with connect() as trace_conn:
        trace_conn.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, actor, "争用", detail, now_iso()),
        )
        trace_conn.commit()


class LockContention(Exception):
    """票锁争用：后到只记争用痕，不写业务。"""

    def __init__(self, lock_key: str, actor: str, detail: str):
        self.lock_key = lock_key
        self.actor = actor
        self.detail = detail
        super().__init__(detail)


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = now_iso()
    row = conn.execute(
        "SELECT holder FROM pier_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        try:
            conn.execute(
                "INSERT INTO pier_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now),
            )
        except sqlite3.IntegrityError:
            # 并发首插撞票锁：本事务回滚释放写锁，争用痕由调用方回滚后补记
            conn.rollback()
            raise LockContention(lock_key, holder, "并发占票锁冲突")
        return True
    if row["holder"] == holder:
        return True
    # 持有者存在：本事务未做写入，调用方回滚后独立记争用痕
    raise LockContention(lock_key, holder, f"持有者={row['holder']}")


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM pier_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    try:
        try_acquire(conn, lock_key, holder)
    except LockContention as exc:
        conn.rollback()
        _record_contention(exc.lock_key, exc.actor, exc.detail)
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pier_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        try:
            try_acquire(conn, body.lock_key, user)
        except LockContention as exc:
            conn.rollback()
            _record_contention(exc.lock_key, exc.actor, exc.detail)
            with connect() as audit_conn:
                write_audit(audit_conn, user, "lock_acquire", body.lock_key, {"ok": False})
                audit_conn.commit()
            raise HTTPException(409, "争用：未能获得锁")
        write_audit(conn, user, "lock_acquire", body.lock_key, {"ok": True})
        conn.commit()
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}