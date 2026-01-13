from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel


# Load local env if present (safe in prod; no-ops if missing)
load_dotenv()


class Settings(BaseModel):
    deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "")
    public_wss_url: str = os.getenv("PUBLIC_WSS_URL", "")
    # Optional: push transcript events to your UI backend (sales-dialer-poc)
    # Example: ws://localhost:8000/ws/transcripts  OR  wss://<ngrok>/ws/transcripts
    transcript_ingest_ws_url: str = os.getenv("TRANSCRIPT_INGEST_WS_URL", "")
    # Rule-based AMD log (JSONL) - stored for future ML use.
    amd_log_path: str = os.getenv("AMD_LOG_PATH", "amd_decisions.jsonl")
    # Keep default aligned with docs/ngrok scripts.
    port: int = int(os.getenv("PORT", "8002"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


settings = Settings()

