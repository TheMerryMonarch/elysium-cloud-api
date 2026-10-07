# app.py (Render / cloud)
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Any, Dict

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# -----------------------------
# Config
# -----------------------------
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "365"))  # 0 = keep forever
DB_PATH = os.getenv("DB_PATH", "/data/elysium.db")    # set to ":memory:" or empty to force in-memory
ALLOWED_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "https://elysiumshrimptank.com,https://www.elysiumshrimptank.com,http://localhost:8000,http://localhost:5000",
).split(",")

# -----------------------------
# Helpers
# -----------------------------
def parse_timestamp(ts: Any) -> datetime:
    """
    Accept:
      - ISO strings like "2025-12-15T20:15:00Z"
      - ISO strings with offset like "2025-12-15T20:15:00+00:00"
      - naive ISO strings "2025-12-15T20:15:00" (assume UTC)
      - datetime objects
    Return: timezone-aware UTC datetime
    """
    if ts is None:
        raise ValueError("timestamp missing")

    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, str):
        s = ts.strip()
        # Convert trailing Z to +00:00 for fromisoformat
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    else:
        raise ValueError(f"unsupported timestamp type: {type(ts)}")

    # Ensure tz-aware in UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt


def to_float_or_none(x: Any) -> Optional[float]:
    if x is None:
        return None
    if x == "":
        return None
    try:
        return float(x)
    except Exception:
        return None


# -----------------------------
# Models
# -----------------------------
class IngestPayload(BaseModel):
    timestamp: Any = Field(..., description="ISO timestamp string or datetime")
    temperature_f: Optional[float] = None
    temp_f: Optional[float] = None

    # Optional sensors
    tds_us_cm: Optional[float] = None
    tds: Optional[float] = None  # legacy name (if you still send it)

    do_mg_per_l: Optional[float] = None
    dissolved_oxygen: Optional[float] = None  # legacy alias if needed
    do_percent: Optional[float] = None

    ph: Optional[float] = None
    orp_mv: Optional[float] = None

    gh: Optional[float] = None
    kh: Optional[float] = None
    light_lux: Optional[float] = None


class Reading(BaseModel):
    timestamp: datetime
    temperature_f: Optional[float] = None
    tds_us_cm: Optional[float] = None
    do_mg_per_l: Optional[float] = None
    do_percent: Optional[float] = None
    ph: Optional[float] = None
    gh: Optional[float] = None
    kh: Optional[float] = None
    light_lux: Optional[float] = None
    orp_mv: Optional[float] = None


# -----------------------------
# App + CORS
# -----------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory cache (always populated; on Starter w/ persistent disk, SQLite is canonical)
_history: List[Reading] = []
_latest: Optional[Reading] = None

# -----------------------------
# Storage backend (SQLite on disk if available, otherwise in-memory only)
# -----------------------------
USE_DB = False
_DB_COLS = ("timestamp", "temperature_f", "tds_us_cm", "do_mg_per_l",
            "do_percent", "ph", "gh", "kh", "light_lux", "orp_mv")
_ingest_count_since_prune = 0


def _try_init_db() -> None:
    global USE_DB
    if not DB_PATH or DB_PATH == ":memory:":
        print("[DB] In-memory only (DB_PATH unset or :memory:)")
        return
    try:
        d = os.path.dirname(DB_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        with sqlite3.connect(DB_PATH) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""
                CREATE TABLE IF NOT EXISTS readings (
                    timestamp TEXT NOT NULL,
                    temperature_f REAL,
                    tds_us_cm REAL,
                    do_mg_per_l REAL,
                    do_percent REAL,
                    ph REAL,
                    gh REAL,
                    kh REAL,
                    light_lux REAL,
                    orp_mv REAL
                )
            """)
            # Columns added after the table first shipped.
            have = {r[1] for r in c.execute("PRAGMA table_info(readings)")}
            if "orp_mv" not in have:
                c.execute("ALTER TABLE readings ADD COLUMN orp_mv REAL")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON readings(timestamp)")
            c.commit()
        USE_DB = True
        print(f"[DB] Persisting to SQLite at {DB_PATH}")
    except Exception as e:
        print(f"[DB] DISABLED — falling back to in-memory ({e})")
        USE_DB = False


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {k: row[k] for k in _DB_COLS}


def _db_insert(reading: Reading) -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            f"INSERT INTO readings ({','.join(_DB_COLS)}) VALUES ({','.join('?' * len(_DB_COLS))})",
            (
                reading.timestamp.isoformat(),
                reading.temperature_f,
                reading.tds_us_cm,
                reading.do_mg_per_l,
                reading.do_percent,
                reading.ph,
                reading.gh,
                reading.kh,
                reading.light_lux,
                reading.orp_mv,
            ),
        )
        c.commit()


def _db_history(cutoff: datetime, limit: int) -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM readings WHERE timestamp >= ? ORDER BY timestamp LIMIT ?",
            (cutoff.isoformat(), limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _db_latest() -> Optional[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM readings ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def _db_prune(cutoff: datetime) -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM readings WHERE timestamp < ?", (cutoff.isoformat(),))
        c.commit()


def _db_count() -> int:
    with sqlite3.connect(DB_PATH) as c:
        return c.execute("SELECT COUNT(*) FROM readings").fetchone()[0]


def prune_history(now_utc: datetime) -> None:
    if HISTORY_DAYS <= 0:
        return
    cutoff = now_utc - timedelta(days=HISTORY_DAYS)
    if USE_DB:
        _db_prune(cutoff)
    else:
        global _history
        _history = [r for r in _history if r.timestamp >= cutoff]


_try_init_db()

# Warm the latest-cache from DB on cold start so /latest is fast
if USE_DB:
    try:
        _latest_row = _db_latest()
        if _latest_row:
            _latest = Reading(**_latest_row)
    except Exception as e:
        print(f"[DB] Warm-cache failed: {e}")


# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health() -> Dict[str, Any]:
    if USE_DB:
        try:
            count = _db_count()
        except Exception as e:
            count = -1
            print(f"[DB] count failed: {e}")
    else:
        count = len(_history)
    return {
        "ok": True,
        "history_days": HISTORY_DAYS,
        "storage": "sqlite" if USE_DB else "in-memory",
        "db_path": DB_PATH if USE_DB else None,
        "count": count,
        "latest_timestamp": (_latest.timestamp.isoformat() if _latest else None),
    }


@app.post("/ingest")
def ingest(payload: IngestPayload) -> Dict[str, Any]:
    global _latest, _history, _ingest_count_since_prune

    try:
        ts = parse_timestamp(payload.timestamp)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad timestamp: {e}")

    # Normalize fields (accept both new + legacy names)
    temp = to_float_or_none(payload.temperature_f)
    if temp is None:
        temp = to_float_or_none(payload.temp_f)

    tds = to_float_or_none(payload.tds_us_cm)
    if tds is None:
        tds = to_float_or_none(payload.tds)

    do = to_float_or_none(payload.do_mg_per_l)
    if do is None:
        do = to_float_or_none(payload.dissolved_oxygen)

    reading = Reading(
        timestamp=ts,
        temperature_f=temp,
        tds_us_cm=tds,
        do_mg_per_l=do,
        do_percent=to_float_or_none(payload.do_percent),
        ph=to_float_or_none(payload.ph),
        gh=to_float_or_none(payload.gh),
        kh=to_float_or_none(payload.kh),
        light_lux=to_float_or_none(payload.light_lux),
        orp_mv=to_float_or_none(payload.orp_mv),
    )

    _latest = reading
    if USE_DB:
        _db_insert(reading)
        _ingest_count_since_prune += 1
        # Prune occasionally — once every ~1000 inserts (~80 min at 5s cadence)
        if _ingest_count_since_prune >= 1000:
            prune_history(datetime.now(timezone.utc))
            _ingest_count_since_prune = 0
    else:
        _history.append(reading)
        prune_history(datetime.now(timezone.utc))

    return {
        "ok": True,
        "stored": 1,
        "latest": reading.model_dump(mode="json"),
    }


# -----------------------------
# MO/TH/ER narration feed
# -----------------------------
NARRATION_TOKEN = os.getenv("NARRATION_TOKEN", "")


class NarrationEntry(BaseModel):
    ts: Any
    text: str


class NarrationPayload(BaseModel):
    entries: List[NarrationEntry]


# In-memory like the readings; the Pi publisher re-sends its recent window
# every few minutes, so the feed self-heals after a restart/redeploy.
_narration: List[Dict[str, Any]] = []


@app.post("/narration")
def narration_ingest(
    payload: NarrationPayload,
    x_narration_token: str = Header(default=""),
) -> Dict[str, Any]:
    if not NARRATION_TOKEN or x_narration_token != NARRATION_TOKEN:
        raise HTTPException(status_code=401, detail="bad narration token")
    global _narration
    merged: Dict[Any, Dict[str, Any]] = {(e["ts"], e["text"]): e for e in _narration}
    for entry in payload.entries:
        try:
            ts = parse_timestamp(entry.ts)
        except Exception:
            continue
        text = entry.text.strip()
        if not text:
            continue
        key = (ts.isoformat(), text)
        merged[key] = {"ts": ts.isoformat(), "text": text}
    _narration = sorted(merged.values(), key=lambda e: e["ts"])[-200:]
    return {"ok": True, "count": len(_narration)}


@app.get("/narration")
def narration(limit: int = 20) -> List[Dict[str, Any]]:
    return _narration[-max(1, min(limit, 100)):]


# -----------------------------
# MO/TH/ER reading ballot
# -----------------------------
# The Pi is authoritative: it pushes the ballot (options, schedule, result) on
# every publish run and pulls the vote counts when voting closes. This app only
# collects votes, one per browser token, with a per-IP cap as a backstop.
# Voter tokens and IPs are stored only as salted hashes. Counts stay hidden
# while voting is open so the vote is not a bandwagon.
BALLOT_SALT = os.getenv("BALLOT_SALT", "") or NARRATION_TOKEN
BALLOT_MAX_PER_IP = int(os.getenv("BALLOT_MAX_PER_IP", "5"))
_VOTER_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

_ballot: Optional[Dict[str, Any]] = None
_votes_mem: Dict[str, Dict[str, Dict[str, str]]] = {}   # in-memory fallback: ballot -> voter -> {option, ip}


class BallotOption(BaseModel):
    id: str
    label: str
    author: str
    work_title: str
    title: str
    continue_: bool = Field(alias="continue")


class BallotPayload(BaseModel):
    id: str
    status: str
    opens_at: str
    closes_at: str
    next_start_at: str
    options: List[BallotOption]
    now_reading: Dict[str, Any]
    result: Optional[Dict[str, Any]] = None


class VotePayload(BaseModel):
    ballot_id: str
    option: str
    voter: str


def _hash(value: str) -> str:
    return hashlib.sha256(f"{BALLOT_SALT}:{value}".encode()).hexdigest()[:32]


def _ballot_db_init() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute("CREATE TABLE IF NOT EXISTS ballot_state (id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL)")
        c.execute("""CREATE TABLE IF NOT EXISTS ballot_votes (
            ballot_id TEXT NOT NULL, voter TEXT NOT NULL, ip TEXT NOT NULL,
            option TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (ballot_id, voter))""")
        c.commit()


def _ballot_load() -> None:
    global _ballot
    if not USE_DB:
        return
    try:
        _ballot_db_init()
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute("SELECT body FROM ballot_state WHERE id = 1").fetchone()
        _ballot = json.loads(row[0]) if row else None
    except Exception as e:
        print(f"[DB] ballot load failed: {e}")


_ballot_load()


def _ballot_status(b: Dict[str, Any]) -> str:
    if b.get("status") in ("tie", "decided"):
        return b["status"]
    now = datetime.now(timezone.utc)
    if now < parse_timestamp(b["opens_at"]):
        return "locked"
    if now < parse_timestamp(b["closes_at"]):
        return "open"
    return "closed"


def _ballot_counts(ballot_id: str) -> Dict[str, int]:
    if USE_DB:
        with sqlite3.connect(DB_PATH) as c:
            rows = c.execute("SELECT option, COUNT(*) FROM ballot_votes WHERE ballot_id = ? GROUP BY option",
                             (ballot_id,)).fetchall()
        return {o: n for o, n in rows}
    counts: Dict[str, int] = {}
    for v in _votes_mem.get(ballot_id, {}).values():
        counts[v["option"]] = counts.get(v["option"], 0) + 1
    return counts


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


@app.post("/ballot")
def ballot_push(payload: BallotPayload, x_narration_token: str = Header(default="")) -> Dict[str, Any]:
    if not NARRATION_TOKEN or x_narration_token != NARRATION_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    global _ballot
    _ballot = payload.model_dump(by_alias=True)
    if USE_DB:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("INSERT OR REPLACE INTO ballot_state (id, body) VALUES (1, ?)", (json.dumps(_ballot),))
            c.commit()
    return {"ok": True, "status": _ballot_status(_ballot)}


@app.get("/ballot")
def ballot_get() -> Dict[str, Any]:
    if not _ballot:
        return {"ballot": None}
    b = dict(_ballot)
    b["status"] = _ballot_status(b)
    if b["status"] in ("closed", "tie", "decided"):
        b["counts"] = _ballot_counts(b["id"])
    return {"ballot": b}


@app.post("/ballot/vote")
def ballot_vote(payload: VotePayload, request: Request) -> Dict[str, Any]:
    if not _ballot or payload.ballot_id != _ballot["id"] or _ballot_status(_ballot) != "open":
        raise HTTPException(status_code=409, detail="voting is not open for this ballot")
    if payload.option not in {o["id"] for o in _ballot["options"]}:
        raise HTTPException(status_code=400, detail="unknown option")
    if not _VOTER_RE.match(payload.voter):
        raise HTTPException(status_code=400, detail="bad voter token")
    voter, ip = _hash(payload.voter), _hash(_client_ip(request))
    bid = payload.ballot_id
    if USE_DB:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute("SELECT option FROM ballot_votes WHERE ballot_id = ? AND voter = ?", (bid, voter)).fetchone()
            if row:
                return {"ok": True, "already": row[0]}
            n_ip = c.execute("SELECT COUNT(*) FROM ballot_votes WHERE ballot_id = ? AND ip = ?", (bid, ip)).fetchone()[0]
            if n_ip >= BALLOT_MAX_PER_IP:
                raise HTTPException(status_code=429, detail="too many votes from this address")
            c.execute("INSERT INTO ballot_votes (ballot_id, voter, ip, option, ts) VALUES (?, ?, ?, ?, ?)",
                      (bid, voter, ip, payload.option, datetime.now(timezone.utc).isoformat()))
            c.commit()
    else:
        votes = _votes_mem.setdefault(bid, {})
        if voter in votes:
            return {"ok": True, "already": votes[voter]["option"]}
        if sum(1 for v in votes.values() if v["ip"] == ip) >= BALLOT_MAX_PER_IP:
            raise HTTPException(status_code=429, detail="too many votes from this address")
        votes[voter] = {"option": payload.option, "ip": ip}
    return {"ok": True, "already": None, "option": payload.option}


@app.get("/ballot/votes")
def ballot_votes(ballot_id: str, x_narration_token: str = Header(default="")) -> Dict[str, Any]:
    if not NARRATION_TOKEN or x_narration_token != NARRATION_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    return {"ballot_id": ballot_id, "counts": _ballot_counts(ballot_id)}


@app.get("/latest")
def latest() -> Dict[str, Any]:
    if _latest:
        return _latest.model_dump(mode="json")
    if USE_DB:
        try:
            row = _db_latest()
            if row:
                return row
        except Exception as e:
            print(f"[DB] /latest failed: {e}")
    return {
        "timestamp": None, "temperature_f": None, "tds_us_cm": None,
        "do_mg_per_l": None, "do_percent": None, "ph": None, "orp_mv": None,
    }


@app.get("/history")
def history(
    hours: int = 24,
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    """
    Returns last N hours of history (default 24).
    You can keep your dashboard simple:
      GET /history?hours=24
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(1, hours))
    safe_limit = max(1, min(limit, 200000))

    if USE_DB:
        try:
            return _db_history(cutoff, safe_limit)
        except Exception as e:
            print(f"[DB] /history failed, falling back to in-memory: {e}")

    rows = [r for r in _history if r.timestamp >= cutoff]
    rows = rows[-max(1, min(limit, 20000)) :]  # safety cap

    return [r.model_dump(mode="json") for r in rows]

