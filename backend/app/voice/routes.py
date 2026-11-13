"""Voice HTTP endpoints — thin wrappers around app.voice.service.

These do NOT run the chat pipeline. The browser calls /api/voice/transcribe to turn a recording
into text, sends that text through the normal /api/chat/stream flow (completely unchanged), then
calls /api/voice/synthesize on the finished answer to get audio back.

Registered on the existing FastAPI app in app/api/main.py, behind the SAME require_api_key
dependency as every other endpoint, so voice inherits the app's auth rather than inventing its own.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.observability import get_logger
from app.voice import service

log = get_logger()
router = APIRouter(prefix="/api/voice", tags=["voice"])

# Defensive cap on an upload. A chat utterance is seconds long; 25 MB is far more than enough
# and stops a malformed or hostile request from buffering something huge into memory.
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


class SynthesizeRequest(BaseModel):
    text: str


@router.get("/status")
async def voice_status() -> dict:
    """Whether voice can run. Never loads a model, so the UI can poll this on page load."""
    return service.status()


# Content-Type -> file suffix, so ffmpeg is told what it is being handed. The browser decides the
# recording format (Chrome/Edge give webm/opus, Safari gives mp4/aac), so this must not assume one.
_CONTENT_TYPE_SUFFIX = {
    "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3", "audio/mp4": ".mp4", "audio/aac": ".aac", "audio/flac": ".flac",
}


@router.post("/transcribe")
async def voice_transcribe(request: Request, lang: str | None = None) -> dict:
    """Speech -> text. The recording is posted as the RAW request body.

    Raw body rather than a multipart file upload, deliberately: FastAPI's UploadFile/File(...)
    requires the `python-multipart` package AT IMPORT TIME, and a missing one raises while the
    route is being declared - which would stop the whole API from starting, not just disable
    voice. Reading the body keeps this package's "never breaks the app" guarantee intact and
    drops a dependency at the same time.
    """
    st = service.status()
    if not st.get("available"):
        raise HTTPException(status_code=503, detail=st.get("reason") or "Voice is not available.")

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio upload.")
    if len(data) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Recording too large.")

    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    filename = "audio" + _CONTENT_TYPE_SUFFIX.get(ctype, ".webm")

    try:
        # run_in_threadpool is REQUIRED, not stylistic: the model call is synchronous and would
        # otherwise block the event loop for the whole transcription, stalling every other
        # request - including an in-flight chat stream.
        text = await run_in_threadpool(service.transcribe, data, filename, lang)
    except Exception as exc:  # noqa: BLE001
        log.warning("voice: transcribe failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")

    log.info("voice: transcribed %d bytes -> %r", len(data), text[:80])
    return {"text": text}


@router.post("/synthesize")
async def voice_synthesize(req: SynthesizeRequest) -> Response:
    """Text -> speech. Returns a WAV the browser can play directly."""
    st = service.status()
    if not st.get("available"):
        raise HTTPException(status_code=503, detail=st.get("reason") or "Voice is not available.")
    if not (req.text or "").strip():
        raise HTTPException(status_code=400, detail="Empty text.")

    try:
        wav_bytes, _sr = await run_in_threadpool(service.synthesize, req.text)
    except Exception as exc:  # noqa: BLE001
        log.warning("voice: synthesize failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Speech synthesis failed: {exc}")

    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )
