"""One-time setup for the optional voice feature:  python scripts/setup_voice.py

Does the three things pip cannot:
  1. checks that ffmpeg is on PATH (a system binary, required to decode browser recordings)
  2. downloads the Piper voice (.onnx + .onnx.json) — piper-tts ships the engine, NOT any voice
  3. optionally pre-downloads the speech-recognition model, so the first person to use the mic
     does not sit through a multi-hundred-MB download mid-question

Safe to re-run: anything already present is left alone.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Console encoding: Windows defaults to cp1252 and the status characters below are not in it, so
# printing would raise UnicodeEncodeError and kill the script mid-download.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings  # noqa: E402
from app.voice import config as cfg  # noqa: E402
from app.voice import service, voices  # noqa: E402


def check_ffmpeg() -> bool:
    print("1. ffmpeg")
    # service._ffmpeg_path(), not shutil.which(): the service also looks in the winget install
    # locations, so a fresh `winget install` is found without reopening the terminal. Using the
    # same lookup keeps this script's verdict identical to what the running server will see.
    path = service._ffmpeg_path()
    if path:
        print(f"   OK  {path}")
        return True
    print("   MISSING - required to decode browser recordings.")
    print("       Windows: winget install Gyan.FFmpeg")
    print("       Debian:  sudo apt-get install -y ffmpeg")
    return False


def fetch_piper_voices() -> bool:
    """Download EVERY catalogue voice, not just the configured one.

    They are ~60 MB each and switching TTS_MODEL in .env is meant to be a one-line change, so
    pre-fetching all of them means a switch needs only a server restart. (The service can also
    fetch a missing voice on demand — this just moves that wait out of someone's first question.)
    """
    print(f"\n2. Piper voices  (configured: {cfg.TTS_MODEL})")
    if cfg.TTS_MODEL not in voices.VOICE_PATHS:
        print(f"   WARNING: TTS_MODEL '{cfg.TTS_MODEL}' is not a known voice.")
        print(f"   Known: {', '.join(voices.known_voices())}")
        return False

    ok = True
    for name in voices.known_voices():
        marker = "  <- configured" if name == cfg.TTS_MODEL else ""
        if voices.is_installed(name, cfg.PIPER_DATA_DIR):
            print(f"   OK  already present: {name}{marker}")
            continue
        print(f"   fetching {name}{marker}")
        try:
            voices.ensure_voice(name, cfg.PIPER_DATA_DIR, progress=True)
            print(f"   OK  {name}")
        except Exception as exc:  # noqa: BLE001
            print(f"   FAILED {name}: {exc}")
            # Only the configured voice is required for voice to work at all.
            if name == cfg.TTS_MODEL:
                ok = False
    return ok


def fetch_stt_model() -> bool:
    """Pre-download the speech-recognition weights so the first mic use is not a long wait."""
    model = cfg.pick_stt_model()
    print(f"\n3. Speech-recognition model '{model}'  (device={cfg.device()})")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("   SKIPPED - faster-whisper is not installed.")
        print("       pip install -r backend/requirements-voice.txt")
        return False
    try:
        print("   downloading / loading (first run can take several minutes) ...")
        WhisperModel(model, device=cfg.device(), compute_type=cfg.compute_type())
        print("   OK  cached and ready")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"   FAILED: {exc}")
        return False


def main() -> int:
    print("=" * 70)
    print(f"Voice setup - {settings.app_name}")
    print("=" * 70)
    print(f"  voice dir : {cfg.PIPER_DATA_DIR}")
    print(f"  TTS voice : {cfg.TTS_MODEL}")
    print(f"  STT model : {cfg.pick_stt_model()} (STT_MODEL={cfg.STT_MODEL})")
    print(f"  device    : {cfg.device()}  (VRAM {cfg.gpu_vram_mb()} MB)")
    print()

    results = [check_ffmpeg(), fetch_piper_voices(), fetch_stt_model()]

    print("\n" + "=" * 70)
    if all(results):
        print("Voice is ready. Restart the API server to pick it up.")
        return 0
    print("Setup incomplete - fix the items marked MISSING/FAILED above, then re-run.")
    print("The chatbot itself is unaffected either way: voice simply stays unavailable.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
