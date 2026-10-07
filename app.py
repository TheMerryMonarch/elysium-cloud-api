# app.py (Render / cloud)
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Any, Dict

from fastapi import FastAPI, Header, HTTPException
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

