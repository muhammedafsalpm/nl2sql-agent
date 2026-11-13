"""The selectable Piper voices, and how to fetch one.

ONE catalogue, used by BOTH scripts/setup_voice.py and the running service, so a voice can never
be "known to setup but unknown at runtime" (or the reverse). Adding a voice means adding one line
here and nothing else.

Piper ships the ENGINE via pip but no voices — each voice is a pair of files that must be
downloaded. That is why changing TTS_MODEL in .env used to fail with "Piper voice file missing":
the name was valid, the files simply were not on disk yet. `ensure_voice()` closes that gap by
fetching a known voice on demand, so editing .env and restarting is all that is required.
"""
from __future__ import annotations

import os
import urllib.request

from app.observability import get_logger

log = get_logger()

# Piper publishes its voices on Hugging Face, one folder per voice.
_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# voice name -> path segment under _BASE.
# The name is exactly what goes in TTS_MODEL, and exactly what the .onnx file is called.
VOICE_PATHS: dict[str, str] = {
    "en_US-ryan-medium": "en/en_US/ryan/medium",      # male, US   (default)
    "en_US-lessac-medium": "en/en_US/lessac/medium",  # female, US
    "en_GB-alan-medium": "en/en_GB/alan/medium",      # male, GB
    "en_GB-cori-medium": "en/en_GB/cori/medium",      # female, GB
}

# Both files are required: the .onnx is the model, the .onnx.json describes its sample rate and
# phoneme mapping. Piper cannot load one without the other.
_SUFFIXES = (".onnx", ".onnx.json")


def known_voices() -> list[str]:
    return sorted(VOICE_PATHS)


def voice_files(name: str, data_dir: str) -> list[str]:
    """Absolute paths of the two files a voice needs."""
    clean = name[:-5] if name.endswith(".onnx") else name
    return [os.path.join(data_dir, clean + suffix) for suffix in _SUFFIXES]


def is_installed(name: str, data_dir: str) -> bool:
    return all(os.path.exists(p) and os.path.getsize(p) > 0 for p in voice_files(name, data_dir))


def _download(url: str, dest: str, progress: bool = False) -> None:
    """Download to a .part file, then rename. A half-written file is never left looking complete."""
    tmp = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with urllib.request.urlopen(url, timeout=180) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = last = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress and total:
                pct = done * 100 // total
                if pct != last and pct % 10 == 0:
                    print(f"      {pct:3d}%  ({done // 1024 // 1024} / {total // 1024 // 1024} MB)",
                          flush=True)
                    last = pct
    os.replace(tmp, dest)


def ensure_voice(name: str, data_dir: str, progress: bool = False) -> None:
    """Make sure `name`'s files are on disk, downloading them if they are not.

    Raises RuntimeError with an actionable message for an unknown name, so a typo in TTS_MODEL
    says which voices ARE available rather than failing with a bare file-not-found.
    """
    clean = name[:-5] if name.endswith(".onnx") else name
    if is_installed(clean, data_dir):
        return

    rel = VOICE_PATHS.get(clean)
    if not rel:
        raise RuntimeError(
            f"Unknown Piper voice '{clean}'. Available: {', '.join(known_voices())}. "
            f"To add another, put its Hugging Face path in app/voice/voices.py."
        )

    log.info("voice: downloading Piper voice '%s' (first use of this voice) ...", clean)
    for path in voice_files(clean, data_dir):
        if os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        url = f"{_BASE}/{rel}/{os.path.basename(path)}"
        if progress:
            print(f"   downloading {os.path.basename(path)} ...")
        _download(url, path, progress=progress)
    log.info("voice: voice '%s' ready", clean)
