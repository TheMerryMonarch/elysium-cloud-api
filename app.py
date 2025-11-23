
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timedelta, timezone
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
def ingest(reading: Reading):
    """Pi will post here; for now we’ll simulate from your Mac."""
    global _latest, _history
    _latest = reading
    _history.append(reading)

    # keep only last 24 hours of data
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    _history[:] = [r for r in _history if r.timestamp >= cutoff]

    return {"status": "ok"}

@app.get("/latest", response_model=Optional[Reading])
def latest():
    return _latest

@app.get("/history", response_model=List[Reading])
def history():
    return _history
