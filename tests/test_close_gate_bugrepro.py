"""假绿夹具 → 拒占回归：原“关盘后仍能新占位”的现网缺陷复现。

修复前本测为绿（缺陷存活、旧套件假绿）；修复后翻转为拒占回归断言：
已关档经候补晋升必须 409 拒占、占位零增、留关盘拒占痕。
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
from db import init_db  # noqa: E402
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


def test_bug_closed_slot_still_accepts_new_hold_via_promote():
    """回归：关盘后经候补晋升不得再占成功（原假绿夹具的翻转断言）。"""
    with TestClient(app) as c:
        h = _login(c)
        _close(c, h, "S-AM-01")
        slot = c.get("/api/slots/S-AM-01", headers=h).json()
        assert slot["status"] == "关闭"
        r = c.post(
            "/api/waitlist/promote", headers=h, json={"wait_id": _seed_wait_id(c, h)}
        )
        # 修复后：已关盘一律拒占，占位零增
        assert r.status_code == 409, r.text
        assert "关盘" in r.json()["detail"]
        holds = c.get("/api/holds", headers=h).json()["items"]
        assert not any(x["slot_code"] == "S-AM-01" for x in holds)
        audits = c.get("/api/audit", headers=h).json()["items"]
        assert any(
            a["action"] == "hold_reject_closed" and a["target"] == "P-WAIT"
            for a in audits
        )
