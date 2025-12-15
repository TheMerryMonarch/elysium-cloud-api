from datetime import datetime, timezone, timedelta
from typing import Any
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware

def to_utc_aware(dt_or_str: Any) -> datetime:
    """
    Convert incoming timestamp (datetime or ISO string) into a timezone-aware UTC datetime.
    Accepts:
      - "2025-12-15T19:55:00Z"
      - "2025-12-15T19:55:00+00:00"
      - naive ISO strings (assumed UTC)
      - datetime objects (naive assumed UTC)
    """
    if isinstance(dt_or_str, datetime):
        dt = dt_or_str
    else:
        s = str(dt_or_str).strip()
        # Handle trailing Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

app = FastAPI(
    title="Elysium Cloud API",
    description="API for shrimp tank telemetry",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # for now; you can tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Reading(BaseModel):
    timestamp: datetime
    temperature_f: float
    # later: ph: Optional[float] = None, etc.

# super-simple in-memory storage for now
_latest: Optional[Reading] = None
_history: List[Reading] = []

@app.get("/health")
def health():
    return {"status": "ok"}
@app.post("/ingest")
def ingest(payload: Reading):
    # Normalize incoming timestamp to UTC-aware
    payload.timestamp = to_utc_aware(payload.timestamp)

    # Normalize any existing history timestamps too (just in case older ones were naive)
    for r in _history:
        r.timestamp = to_utc_aware(r.timestamp)

    # Prune using UTC-aware cutoff
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    _history[:] = [r for r in _history if r.timestamp >= cutoff]

    # Append + set latest
    _history.append(payload)
    _latest.clear()
    _latest.update(payload.model_dump() if hasattr(payload, "model_dump") else payload.dict())

    return {"ok": True}


@app.get("/latest", response_model=Optional[Reading])
def latest():
    return _latest

@app.get("/history", response_model=List[Reading])
def history():
    return _history
