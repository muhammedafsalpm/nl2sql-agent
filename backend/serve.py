"""Start the HTTP API:  python serve.py

Equivalent to:
    uvicorn app.api.main:app --host 0.0.0.0 --port 8000

The API is stateless (the client sends conversation history), so it is safe to scale out.
Each worker loads its own schema + agent graph at startup, so more workers cost more memory.
"""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.api.main:app",
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", "8000")),
        reload=os.getenv("API_RELOAD", "false").strip().lower() in ("1", "true", "yes"),
        workers=int(os.getenv("API_WORKERS", "1")),
    )
