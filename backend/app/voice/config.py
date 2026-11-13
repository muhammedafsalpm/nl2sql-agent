"""Voice settings, read from the environment.

DELIBERATELY SEPARATE from app/config.py. That module is a frozen dataclass built once at import
and read by every agent; adding voice fields to it would mean editing a file the whole pipeline
depends on. Keeping the voice settings here means the chat pipeline's configuration is untouched
by this feature, and deleting the `voice` package removes every trace of it.

Every value has a working default, so an .env with no VOICE_* lines at all behaves correctly.
"""
from __future__ import annotations

import os
import shutil
import subprocess


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# Master switch. "0" disables voice completely: the endpoints report unavailable and the UI hides
# the mic. Same convention as a blank API_KEY / LANGFUSE_PUBLIC_KEY elsewhere in this project.
VOICE_ENABLED = _get("VOICE_ENABLED", "1") not in ("0", "false", "no")

# Speech-to-Text. "auto" sizes the model from the hardware (see pick_stt_model below); any
# faster-whisper size name — tiny, base, small, medium, large-v3 — pins it instead.
STT_MODEL = _get("STT_MODEL", "auto")

# Text-to-Speech: a Piper VOICE NAME, not a HuggingFace repo. Its <name>.onnx and <name>.onnx.json
# must exist in PIPER_DATA_DIR (scripts/setup_voice.py downloads them).
TTS_MODEL = _get("TTS_MODEL", "en_US-ryan-medium")

VOICE_LANG = _get("VOICE_LANG", "en")

# auto | cuda | cpu
DEVICE_PREF = _get("VOICE_DEVICE", "auto")

# Default sits inside the repo so a fresh clone + setup script works with no .env edit at all.
_DEFAULT_PIPER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "models", "piper"
)
PIPER_DATA_DIR = _get("PIPER_DATA_DIR") or _DEFAULT_PIPER_DIR


def gpu_vram_mb() -> int:
    """Total VRAM of the first NVIDIA GPU in MB, or 0 when there is no usable GPU.

    Uses nvidia-smi rather than torch: faster-whisper runs on CTranslate2 and this project does
    NOT install torch (that is most of why it was chosen over a PyTorch-based STT). Shelling out
    to the driver keeps the dependency list at 13 packages instead of pulling in ~2.5 GB of torch
    purely to answer one question.
    """
    if not shutil.which("nvidia-smi"):
        return 0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        first = out.stdout.strip().splitlines()[0]
        return int(float(first.strip()))
    except Exception:  # noqa: BLE001 - a hardware probe must never break startup
        return 0


def device() -> str:
    """'cuda' or 'cpu'. An explicit VOICE_DEVICE always wins."""
    if DEVICE_PREF in ("cuda", "cpu"):
        return DEVICE_PREF
    return "cuda" if gpu_vram_mb() > 0 else "cpu"


# VRAM thresholds for STT_MODEL=auto, in MB. Sized against real hardware rather than guessed:
#   * large-v3 needs ~3 GB of weights, so an 8 GB floor leaves room for the display and activations
#   * a 4 GB laptop card (e.g. RTX 3050 Laptop) cannot hold large-v3 at all — Windows reserves
#     several hundred MB for the desktop — so it gets `small`, which fits with room to spare
#   * with no GPU, `base` is the largest size that still transcribes comfortably faster than a
#     person can speak on a normal laptop CPU
_AUTO_TIERS = ((8192, "large-v3"), (4096, "small"))
_AUTO_CPU = "base"


def pick_stt_model() -> str:
    """The faster-whisper size to load. An explicit STT_MODEL is honoured verbatim."""
    if STT_MODEL and STT_MODEL != "auto":
        return STT_MODEL
    if device() == "cpu":
        return _AUTO_CPU
    vram = gpu_vram_mb()
    for floor, name in _AUTO_TIERS:
        if vram >= floor:
            return name
    return _AUTO_CPU


def compute_type() -> str:
    """CTranslate2 quantisation. float16 on GPU; int8 on CPU, which is what makes CPU usable."""
    return "float16" if device() == "cuda" else "int8"
