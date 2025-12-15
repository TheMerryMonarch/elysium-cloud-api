# app.py (Render / cloud)
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# -----------------------------
# Config
# -----------------------------
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "30"))  # cloud retention (in-memory)
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

    gh: Optional[float] = None
    kh: Optional[float] = None
    light_lux: Optional[float] = None


class Reading(BaseModel):
    timestamp: datetime
    temperature_f: Optional[float] = None
    tds_us_cm: Optional[float] = None
    do_mg_per_l: Optional[float] = None
    gh: Optional[float] = None
    kh: Optional[float] = None
    light_lux: Optional[float] = None


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

# In-memory store (Render disk is ephemeral; this is for live dashboard)
_history: List[Reading] = []
_latest: Optional[Reading] = None


def prune_history(now_utc: datetime) -> None:
    cutoff = now_utc - timedelta(days=HISTORY_DAYS)
    # All timestamps are forced to tz-aware UTC, so comparisons are safe
    global _history
    _history = [r for r in _history if r.timestamp >= cutoff]


# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "history_days": HISTORY_DAYS,
        "count": len(_history),
        "latest_timestamp": (_latest.timestamp.isoformat() if _latest else None),
    }


@app.post("/ingest")
def ingest(payload: IngestPayload) -> Dict[str, Any]:
    global _latest, _history

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
        gh=to_float_or_none(payload.gh),
        kh=to_float_or_none(payload.kh),
        light_lux=to_float_or_none(payload.light_lux),
    )

    _latest = reading
    _history.append(reading)

    prune_history(datetime.now(timezone.utc))

    return {
        "ok": True,
        "stored": 1,
        "latest": reading.model_dump(mode="json"),
        "count": len(_history),
    }


@app.get("/latest")
def latest() -> Dict[str, Any]:
    if not _latest:
        return {"timestamp": None, "temperature_f": None, "tds_us_cm": None, "do_mg_per_l": None}
    return _latest.model_dump(mode="json")


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

    rows = [r for r in _history if r.timestamp >= cutoff]
    rows = rows[-max(1, min(limit, 20000)) :]  # safety cap

    return [r.model_dump(mode="json") for r in rows]

