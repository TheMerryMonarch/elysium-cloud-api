"""Ballot endpoints. Run: pip install fastapi httpx pytest tzdata && pytest -q"""
import importlib
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

TOKEN = "tok"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("NARRATION_TOKEN", TOKEN)
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import app as app_module
    app_module = importlib.reload(app_module)
    return TestClient(app_module.app), app_module


def _ballot(opens_delta_h=-1, closes_delta_h=48, status="open", bid="2026-10-08-republic-0"):
    now = datetime.now(timezone.utc)
    return {
        "id": bid, "status": status,
        "opens_at": (now + timedelta(hours=opens_delta_h)).isoformat(),
        "closes_at": (now + timedelta(hours=closes_delta_h)).isoformat(),
        "next_start_at": (now + timedelta(hours=closes_delta_h + 24)).isoformat(),
        "options": [
            {"id": "a", "label": "Continue the story: The Republic, Books III and IV", "author": "Plato",
             "work_title": "The Republic", "title": "Books III and IV", "continue": True},
            {"id": "b", "label": "Kant, Critique of Pure Reason: Prefaces", "author": "Kant",
             "work_title": "Critique of Pure Reason", "title": "Prefaces", "continue": False},
            {"id": "c", "label": "Laozi, Tao Te Ching: Chapters 1 to 81", "author": "Laozi",
             "work_title": "Tao Te Ching", "title": "Chapters 1 to 81", "continue": False},
        ],
        "now_reading": {"author": "Plato", "work_title": "The Republic", "title": "Books I and II", "day": 4, "days": 10},
        "result": None,
    }


def _push(c, b):
    return c.post("/ballot", json=b, headers={"X-Narration-Token": TOKEN})


def _vote(c, option="a", voter="v" * 32, bid="2026-10-08-republic-0", ip="1.2.3.4"):
    return c.post("/ballot/vote", json={"ballot_id": bid, "option": option, "voter": voter},
                  headers={"X-Forwarded-For": f"{ip}, 10.0.0.1"})


def test_push_requires_token(client):
    c, _ = client
    assert c.post("/ballot", json=_ballot()).status_code == 401


def test_get_ballot_before_any_push_is_empty(client):
    c, _ = client
    assert c.get("/ballot").json() == {"ballot": None}


def test_status_is_computed_from_server_time(client):
    c, _ = client
    _push(c, _ballot(opens_delta_h=5, status="open"))     # Pi thinks open; server clock says not yet
    assert c.get("/ballot").json()["ballot"]["status"] == "locked"
    _push(c, _ballot(opens_delta_h=-1, closes_delta_h=-0.5))
    assert c.get("/ballot").json()["ballot"]["status"] == "closed"


def test_vote_counts_once_per_voter(client):
    c, _ = client
    _push(c, _ballot())
    assert _vote(c, "b").json()["ok"] is True
    again = _vote(c, "c")
    assert again.status_code == 200 and again.json()["already"] == "b"
    counts = c.get("/ballot/votes", params={"ballot_id": "2026-10-08-republic-0"},
                   headers={"X-Narration-Token": TOKEN}).json()["counts"]
    assert counts == {"b": 1}


def test_counts_hidden_while_open_and_shown_after_close(client):
    c, _ = client
    _push(c, _ballot())
    _vote(c, "a")
    assert "counts" not in c.get("/ballot").json()["ballot"]
    _push(c, _ballot(opens_delta_h=-48, closes_delta_h=-1))
    assert c.get("/ballot").json()["ballot"]["counts"] == {"a": 1}


def test_vote_rejected_when_locked_or_closed(client):
    c, _ = client
    _push(c, _ballot(opens_delta_h=3))
    assert _vote(c).status_code == 409
    _push(c, _ballot(opens_delta_h=-48, closes_delta_h=-1))
    assert _vote(c).status_code == 409


def test_vote_rejects_unknown_option_and_stale_ballot(client):
    c, _ = client
    _push(c, _ballot())
    assert _vote(c, "z").status_code == 400
    assert _vote(c, "a", bid="old-ballot").status_code == 409


def test_ip_cap_limits_one_address(client):
    c, m = client
    _push(c, _ballot())
    codes = [_vote(c, "a", voter=f"{i:032d}").status_code for i in range(m.BALLOT_MAX_PER_IP + 1)]
    assert codes[:-1] == [200] * m.BALLOT_MAX_PER_IP and codes[-1] == 429
    assert _vote(c, "a", voter="x" * 32, ip="5.6.7.8").status_code == 200


def test_voter_token_must_look_random(client):
    c, _ = client
    _push(c, _ballot())
    assert _vote(c, "a", voter="short").status_code == 400


def test_votes_and_ballot_survive_restart(client, monkeypatch):
    c, m = client
    _push(c, _ballot())
    _vote(c, "c")
    m2 = importlib.reload(m)
    c2 = TestClient(m2.app)
    assert c2.get("/ballot").json()["ballot"]["id"] == "2026-10-08-republic-0"
    counts = c2.get("/ballot/votes", params={"ballot_id": "2026-10-08-republic-0"},
                    headers={"X-Narration-Token": TOKEN}).json()["counts"]
    assert counts == {"c": 1}


def test_raw_ips_and_voter_tokens_are_not_stored(client, tmp_path):
    c, _ = client
    _push(c, _ballot())
    _vote(c, "a", voter="secretvoter" * 3, ip="9.9.9.9")
    blob = (tmp_path / "t.db").read_bytes()
    assert b"9.9.9.9" not in blob and b"secretvoter" not in blob


def test_pi_tie_and_decided_status_win_over_clock(client):
    c, _ = client
    b = _ballot(opens_delta_h=-48, closes_delta_h=-1, status="decided")
    b["result"] = {"winner": "b", "decided_by": "votes", "counts": {"b": 3}, "reason": None}
    _push(c, b)
    got = c.get("/ballot").json()["ballot"]
    assert got["status"] == "decided" and got["result"]["winner"] == "b"


def test_in_memory_fallback_votes_once(monkeypatch):
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("NARRATION_TOKEN", TOKEN)
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import app as m
    m = importlib.reload(m)
    c = TestClient(m.app)
    _push(c, _ballot())
    assert _vote(c, "b").json()["already"] is None
    assert _vote(c, "c").json()["already"] == "b"
    assert c.get("/ballot/votes", params={"ballot_id": "2026-10-08-republic-0"},
                 headers={"X-Narration-Token": TOKEN}).json()["counts"] == {"b": 1}
