from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware


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
    # Parse incoming timestamp safely (UTC-aware)
    ts = payload.timestamp
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    payload.timestamp = dt

    # Prune history using UTC-aware cutoff
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    _history[:] = [r for r in _history if r.timestamp >= cutoff]

    _history.append(payload)
    _latest.clear()
    _latest.update(payload.dict())

    return {"ok": True}

@app.get("/latest", response_model=Optional[Reading])
def latest():
    return _latest

@app.get("/history", response_model=List[Reading])
def history():
    return _history
