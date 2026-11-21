"""关盘拒占：关盘仍占失败测 + 双人对关盘争用失败测。

口径（甲路径·关盘校验整笔回滚，见 docs/关盘拒占说明.md）：
- 占位写口（/api/holds、/api/waitlist/promote）在档锁内读到已关盘
  → 占位零增、整笔回滚、留关盘拒占痕（audit: hold_reject_closed）后 409 拒写；
- 两人同时对已关档占位 → 抢档锁 slot:{code}，后到只留争用痕（contention_log）；
- 关盘态、占位结果、余席、对账、审计五处同态。
夹具跑本机文件库（tmp 文件库，非内存库）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
from db import connect, init_db  # noqa: E402
import db as dbmod  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"  # 本机文件库
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c, user="admin", password="123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _close(c, h, code):
    r = c.post("/api/slots/close", headers=h, json={"code": code})
    assert r.status_code == 200, r.text


def _seed_wait_id(c, h):
    return [
        w
        for w in c.get("/api/waitlist", headers=h).json()["items"]
        if w["party_key"] == "P-WAIT" and w["status"] == "候补中"
    ][0]["id"]


def test_closed_slot_rejects_new_hold():
    """关盘仍占失败测：两个占位写口都必须拒占，且五处同态。"""
    with TestClient(app) as c:
        h = _login(c)
        _close(c, h, "S-AM-01")

        # 写口一：直接占位 → 拒
        r1 = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": "S-AM-01", "party_key": "P-NEW", "seats": 1},
        )
        assert r1.status_code == 409, r1.text
        assert "关盘" in r1.json()["detail"]

        # 写口二：候补晋升 → 同样拒（缺陷所在）
        r2 = c.post(
            "/api/waitlist/promote", headers=h, json={"wait_id": _seed_wait_id(c, h)}
        )
        assert r2.status_code == 409, r2.text
        assert "关盘" in r2.json()["detail"]

        # 占位结果：零增，无新占位行；候补仍候补中（整笔回滚，不留半截）
        holds = c.get("/api/holds", headers=h).json()["items"]
        assert not any(x["slot_code"] == "S-AM-01" for x in holds)
        waits = c.get("/api/waitlist", headers=h).json()["items"]
        p_wait = [w for w in waits if w["party_key"] == "P-WAIT"][0]
        assert p_wait["status"] == "候补中"

        # 关盘态与余席：仍关闭，已订/容量不动（种子 booked=2 / capacity=10）
        slot = c.get("/api/slots/S-AM-01", headers=h).json()
        assert slot["status"] == "关闭"
        assert slot["booked"] == 2
        assert slot["capacity"] == 10

        # 对账：已关档无占位中，同源校验通过
        recon = c.get("/api/reconcile", headers=h).json()
        assert recon["all_ok"] is True
        item = [i for i in recon["items"] if i["slot_code"] == "S-AM-01"][0]
        assert item["ok"] is True
        assert item["open_holds"] == 0

        # 审计：两笔拒占都留关盘拒占痕
        audits = c.get("/api/audit", headers=h).json()["items"]
        rejected = {
            (a["action"], a["target"]) for a in audits if a["action"] == "hold_reject_closed"
        }
        assert ("hold_reject_closed", "P-NEW") in rejected
        assert ("hold_reject_closed", "P-WAIT") in rejected


def test_closed_slot_contention_late_comer_only_leaves_trace():
    """双人对关盘争用失败测：两人抢已关档，后到只留争用痕，不写业务。"""
    with TestClient(app) as c:
        admin = _login(c, "admin")
        clerk = _login(c, "clerk")
        _close(c, admin, "S-EVE-03")

        # 先到者持档锁（模拟其占位写口在途）
        r = c.post(
            "/api/locks/acquire", headers=admin, json={"lock_key": "slot:S-EVE-03"}
        )
        assert r.status_code == 200, r.text

        # 后到者对同一已关档占位 → 争用拒写
        r2 = c.post(
            "/api/holds",
            headers=clerk,
            json={"slot_code": "S-EVE-03", "party_key": "P-LATE", "seats": 1},
        )
        assert r2.status_code == 409, r2.text
        assert "争用" in r2.json()["detail"]

        # 只留争用痕：contention_log 必须落库（不得随回滚吞掉）
        contention = c.get("/api/contention", headers=admin).json()["items"]
        assert any(
            x["lock_key"] == "slot:S-EVE-03" and x["actor"] == "clerk" and x["action"] == "争用"
            for x in contention
        )

        # 不写业务：无占位行、无候补行、无 hold_create 审计
        holds = c.get("/api/holds", headers=admin).json()["items"]
        assert not any(x["party_key"] == "P-LATE" for x in holds)
        waits = c.get("/api/waitlist", headers=admin).json()["items"]
        assert not any(x["party_key"] == "P-LATE" for x in waits)
        audits = c.get("/api/audit", headers=admin).json()["items"]
        assert not any(
            a["action"] == "hold_create" and a["actor"] == "clerk" for a in audits
        )

        # 档锁仍归先到者；余席未动
        locks = c.get("/api/locks", headers=admin).json()["items"]
        assert any(
            x["lock_key"] == "slot:S-EVE-03" and x["holder"] == "admin" for x in locks
        )
        slot = c.get("/api/slots/S-EVE-03", headers=admin).json()
        assert slot["status"] == "关闭"
        assert slot["booked"] == 0


def test_reconcile_flags_closed_slot_with_open_hold():
    """对账与关盘态同态：已关档若出现占位中（实写对不上），对账必须判异常。"""
    with TestClient(app) as c:
        h = _login(c)
        _close(c, h, "S-AM-01")
        # 直插脏态：模拟写口失守后已关档残留占位中（绕过 API，仅造数）
        with connect() as conn:
            sid = conn.execute(
                "SELECT id FROM slots WHERE code='S-AM-01'"
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO holds(slot_id,party_key,seats,status,expire_at,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (sid, "P-DIRTY", 1, "占位中", "2026-12-31T23:59:59", "2026-11-21T10:00:00"),
            )
            conn.commit()

        recon = c.get("/api/reconcile", headers=h).json()
        assert recon["all_ok"] is False
        item = [i for i in recon["items"] if i["slot_code"] == "S-AM-01"][0]
        assert item["status"] == "关闭"
        assert item["open_holds"] == 1
        assert item["ok"] is False

        one = c.get("/api/reconcile/S-AM-01", headers=h).json()
        assert one["open_holds"] == 1
        assert one["ok"] is False
