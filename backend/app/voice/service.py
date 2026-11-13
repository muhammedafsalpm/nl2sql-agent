"""Speech-to-Text and Text-to-Speech for the chat UI.

VOICE IS A WRAPPER, NOT A PIPELINE CHANGE
-----------------------------------------
Nothing in this module touches the agent graph. Speech becomes text, that text goes through the
EXACT same /api/chat/stream flow (orchestrator -> sql_author -> validator -> executor -> verifier
-> synthesize), and the finished answer text is turned back into audio. Retrieval, SQL generation
and answering are byte-identical whether the question arrived typed or spoken.

WHY THE REST OF THE APP CANNOT BE AFFECTED
------------------------------------------
  * The ML libraries are imported LAZILY, inside the loaders — never at module import. If
    faster-whisper / piper are not installed, importing this module still succeeds and the
    endpoints report "unavailable"; the chatbot runs exactly as before.
  * Models are loaded once (singletons) behind a lock, on first use, so server startup time is
    unchanged and two concurrent requests cannot load the same model twice.
  * Every failure path returns a clean error. Nothing here can crash a chat turn.

MODELS
------
  * STT: faster-whisper (CTranslate2). Chosen over a PyTorch ASR because it runs usably on CPU
    AND GPU from one code path, needs no torch, and its smaller sizes fit a 4 GB laptop card.
    The size is picked from the hardware — see config.pick_stt_model().
  * TTS: Piper (a small local ONNX voice, en_US-ryan-medium). CPU-friendly everywhere.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import threading

from app.observability import get_logger
from app.voice import config as cfg
from app.voice import voices

log = get_logger()

# ── Lazily-loaded singletons ─────────────────────────────────────────────────────
_stt = None
_stt_lock = threading.Lock()
_stt_error: str | None = None   # remembered, so a hopeless import is not retried every request

_tts = None
_tts_lock = threading.Lock()
_tts_error: str | None = None


_ffmpeg_cache: str | None = None


def _ffmpeg_path() -> str | None:
    """Full path to ffmpeg, or None.

    PATH alone is not enough on Windows. `winget install Gyan.FFmpeg` edits the *user* PATH, but a
    terminal that was already open keeps the copy it started with — and so does any server
    launched from it. The result is a machine where ffmpeg is installed and working, yet voice
    reports "ffmpeg is not installed" until someone happens to restart the right terminal.

    So: check PATH first, then the places winget actually puts it. Anything found is invoked by
    ABSOLUTE path, which sidesteps the stale-PATH problem entirely.
    """
    global _ffmpeg_cache
    if _ffmpeg_cache is not None:
        return _ffmpeg_cache or None

    explicit = os.environ.get("FFMPEG_BINARY", "").strip()
    if explicit and os.path.isfile(explicit):
        _ffmpeg_cache = explicit
        return explicit

    found = shutil.which("ffmpeg")
    if found:
        _ffmpeg_cache = found
        return found

    if os.name == "nt":
        import glob

        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
            *glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                    "Gyan.FFmpeg*", "**", "bin", "ffmpeg.exe"), recursive=True),
            r"C:\ffmpeg\bin\ffmpeg.exe",
        ]
        for path in candidates:
            if os.path.isfile(path):
                log.info("voice: found ffmpeg outside PATH at %s", path)
                _ffmpeg_cache = path
                return path

    _ffmpeg_cache = ""   # remembered, so the filesystem scan runs once, not per request
    return None


def _packages_present() -> tuple[bool, str]:
    """Cheap import-spec check — no model is loaded, so /voice/status stays instant."""
    import importlib.util

    missing = []
    if importlib.util.find_spec("faster_whisper") is None:
        missing.append("faster-whisper (speech recognition)")
    if importlib.util.find_spec("piper") is None:
        missing.append("piper-tts (speech synthesis)")
    if missing:
        return False, "Voice packages not installed: " + ", ".join(missing)
    return True, ""


def status() -> dict:
    """Whether voice can run, WITHOUT loading anything heavy.

    ffmpeg is checked here as well as the Python packages: it is a system binary, it is genuinely
    required to decode a browser recording, and a missing one otherwise only surfaces as a failed
    transcription after the user has already spoken.
    """
    if not cfg.VOICE_ENABLED:
        return {"available": False, "reason": "Voice is disabled (VOICE_ENABLED=0)."}

    ok, reason = _packages_present()
    if not ok:
        return {"available": False, "reason": reason}

    if not _ffmpeg_path():
        return {"available": False,
                "reason": "ffmpeg was not found - required to decode recorded audio. Install it "
                          "(winget install Gyan.FFmpeg) and restart this server, or set "
                          "FFMPEG_BINARY to its full path."}

    # A voice that is not on disk yet is NOT a failure, as long as it is one we know how to fetch:
    # _load_tts downloads it on first use. Only an unrecognised TTS_MODEL is genuinely unavailable,
    # and then the message lists the valid names rather than a bare missing-file path.
    if cfg.TTS_MODEL not in voices.VOICE_PATHS and not voices.is_installed(
        cfg.TTS_MODEL, cfg.PIPER_DATA_DIR
    ):
        return {"available": False,
                "reason": f"Unknown TTS_MODEL '{cfg.TTS_MODEL}'. Available voices: "
                          f"{', '.join(voices.known_voices())}."}

    return {
        "available": True,
        "reason": "",
        "stt_model": cfg.pick_stt_model(),
        "tts_model": cfg.TTS_MODEL,
        "device": cfg.device(),
        "lang": cfg.VOICE_LANG,
    }


# ── Audio conversion ─────────────────────────────────────────────────────────────
def _to_wav_16k_mono(audio_bytes: bytes, src_suffix: str) -> str:
    """Browser recordings arrive as webm/opus; the STT wants 16 kHz mono WAV. Returns a temp path."""
    in_fd, in_path = tempfile.mkstemp(suffix=src_suffix or ".webm")
    out_fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(in_fd)
    os.close(out_fd)
    with open(in_path, "wb") as fh:
        fh.write(audio_bytes)
    exe = _ffmpeg_path()
    if not exe:
        raise RuntimeError("ffmpeg was not found - required to decode recorded audio.")
    try:
        # Absolute path, not the bare name: the server may have been started from a shell whose
        # PATH predates the ffmpeg install (see _ffmpeg_path).
        subprocess.run(
            [exe, "-y", "-i", in_path, "-ar", "16000", "-ac", "1", "-f", "wav", out_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg was not found - required to decode recorded audio.")
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "ignore")[-300:]
        raise RuntimeError(f"Audio conversion failed: {detail}")
    finally:
        try:
            os.remove(in_path)
        except OSError:
            pass
    return out_path


# ── Speech-to-Text ───────────────────────────────────────────────────────────────
# Set once a GPU attempt has failed, so every later request goes straight to CPU instead of
# repeating a load that cannot work.
_stt_forced_cpu = False


_dll_dirs_registered = False


def _register_cuda_dll_dirs() -> None:
    """Windows only: make the CUDA DLLs from the nvidia-*-cu12 wheels loadable.

    CTranslate2 needs cuBLAS and cuDNN, which the NVIDIA driver does NOT ship. The usual way to
    get them without a full CUDA toolkit is `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`,
    but on Windows those land in site-packages/nvidia/*/bin, which is not a DLL search path — so
    CTranslate2 raises "cublas64_12.dll is not found" and the GPU sits unused while everything
    silently runs on CPU. Registering the directories here fixes that with no manual PATH setup.

    A no-op on Linux (add_dll_directory does not exist) and harmless when the wheels are absent.
    """
    global _dll_dirs_registered
    if _dll_dirs_registered or not hasattr(os, "add_dll_directory"):
        return
    _dll_dirs_registered = True
    try:
        import site

        for root in site.getsitepackages():
            nvidia = os.path.join(root, "nvidia")
            if not os.path.isdir(nvidia):
                continue
            for pkg in os.listdir(nvidia):
                bin_dir = os.path.join(nvidia, pkg, "bin")
                if os.path.isdir(bin_dir):
                    os.add_dll_directory(bin_dir)
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    except Exception as exc:  # noqa: BLE001 - best-effort; CPU fallback still covers us
        log.info("voice: could not register CUDA dll dirs (%s)", exc)


def _build_stt(device: str):
    if device == "cuda":
        _register_cuda_dll_dirs()

    from faster_whisper import WhisperModel

    name = cfg.pick_stt_model()
    ct = "float16" if device == "cuda" else "int8"
    # A GPU-sized model on CPU would be unusably slow, so drop to the CPU tier when falling back.
    if device == "cpu" and cfg.STT_MODEL in ("", "auto") and name not in ("tiny", "base", "small"):
        name = "small"
    log.info("voice: loading STT '%s' on %s (%s) ...", name, device, ct)
    model = WhisperModel(name, device=device, compute_type=ct)
    log.info("voice: STT ready (%s on %s)", name, device)
    return model


def _is_cuda_problem(exc: Exception) -> bool:
    """A missing CUDA runtime looks like a normal RuntimeError, so match on the message.

    CTranslate2 links against cuBLAS/cuDNN, which the NVIDIA *driver* alone does not provide —
    they arrive with the CUDA toolkit or the nvidia-*-cu12 wheels. A machine can therefore have a
    working GPU and still be unable to run GPU inference, and it fails at the first encode() call
    rather than at model construction.
    """
    msg = str(exc).lower()
    return any(k in msg for k in ("cublas", "cudnn", "cuda", "libcu", "gpu", "device"))


def _load_stt(force_cpu: bool = False):
    global _stt, _stt_error
    if _stt is not None:
        return _stt
    if _stt_error is not None:
        raise RuntimeError(_stt_error)
    with _stt_lock:
        if _stt is not None:
            return _stt
        device = "cpu" if (force_cpu or _stt_forced_cpu) else cfg.device()
        try:
            _stt = _build_stt(device)
            return _stt
        except Exception as exc:  # noqa: BLE001
            # A GPU that cannot actually run must not disable voice: retry once on CPU. This is
            # what makes one build work on a workstation, a 4 GB laptop and a CPU-only box.
            if device == "cuda" and _is_cuda_problem(exc):
                log.warning("voice: GPU unavailable for STT (%s) - falling back to CPU", exc)
                try:
                    _stt = _build_stt("cpu")
                    return _stt
                except Exception as cpu_exc:  # noqa: BLE001
                    exc = cpu_exc
            _stt_error = f"Failed to load STT model ({cfg.pick_stt_model()}): {exc}"
            log.warning("voice: %s", _stt_error)
            raise RuntimeError(_stt_error)


def _run_stt(model, wav_path: str, lang: str | None) -> str:
    language = (lang or cfg.VOICE_LANG or "").strip().lower() or None
    # `segments` is a GENERATOR - the transcription only actually runs while it is consumed, so
    # this join is where the work (and any CUDA failure) happens, not the transcribe() call.
    segments, _info = model.transcribe(wav_path, language=language, beam_size=5)
    return " ".join(seg.text.strip() for seg in segments).strip()


def transcribe(audio_bytes: bytes, filename: str = "audio.webm", lang: str | None = None) -> str:
    """Speech -> text. Synchronous: call it from a threadpool so the event loop keeps serving."""
    global _stt, _stt_forced_cpu

    model = _load_stt()
    suffix = os.path.splitext(filename or "")[1].lower() or ".webm"
    wav_path = _to_wav_16k_mono(audio_bytes, suffix)
    try:
        try:
            return _run_stt(model, wav_path, lang)
        except Exception as exc:  # noqa: BLE001
            # CTranslate2 only touches the GPU on the first encode(), so a missing cuBLAS/cuDNN
            # surfaces HERE rather than at load time. Rebuild on CPU and retry once; the flag
            # makes every later request go straight to CPU instead of repeating this.
            if _stt_forced_cpu or not _is_cuda_problem(exc):
                raise
            log.warning("voice: STT failed on GPU (%s) - retrying on CPU", exc)
            with _stt_lock:
                _stt_forced_cpu = True
                _stt = None
            model = _load_stt(force_cpu=True)
            return _run_stt(model, wav_path, lang)
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


# ── Text-to-Speech ───────────────────────────────────────────────────────────────
def _load_tts():
    global _tts, _tts_error
    if _tts is not None:
        return _tts
    if _tts_error is not None:
        raise RuntimeError(_tts_error)
    with _tts_lock:
        if _tts is not None:
            return _tts
        try:
            from piper import PiperVoice

            name = cfg.TTS_MODEL[:-5] if cfg.TTS_MODEL.endswith(".onnx") else cfg.TTS_MODEL
            # Fetch the voice if this is the first use of it. Without this, changing TTS_MODEL in
            # .env failed until someone remembered to re-run setup_voice.py — the name was valid,
            # the files just were not downloaded yet. One-time, ~60 MB, inside the existing lock so
            # concurrent requests cannot start two downloads of the same voice.
            voices.ensure_voice(name, cfg.PIPER_DATA_DIR)
            onnx = os.path.join(cfg.PIPER_DATA_DIR, name + ".onnx")
            use_cuda = cfg.device() == "cuda"
            log.info("voice: loading TTS voice '%s' (cuda=%s) ...", name, use_cuda)
            try:
                _tts = PiperVoice.load(onnx, use_cuda=use_cuda)
            except TypeError:
                # Older piper signature has no use_cuda parameter.
                _tts = PiperVoice.load(onnx)
            log.info("voice: TTS ready")
            return _tts
        except Exception as exc:  # noqa: BLE001
            _tts_error = f"Failed to load TTS voice ({cfg.TTS_MODEL}): {exc}"
            log.warning("voice: %s", _tts_error)
            raise RuntimeError(_tts_error)


# Piper slurs a single over-long utterance, and en_US-ryan-medium is a fast voice where it is
# worst. That matters more here than in a general chatbot: answers from this pipeline are often
# long, list-heavy paragraphs ("Showing the first 20 of 224 completed orders: ..."). Splitting an
# over-long sentence into clause-sized pieces keeps every utterance short and clear.
#
# 180 chars is the threshold proven in the reference implementation. Normal, well-punctuated
# sentences fall under it and take the unchanged whole-text path below, so ordinary replies are
# byte-identical to what Piper would produce without any of this.
_MAX_SEGMENT_CHARS = 180


def _needs_segmentation(text: str, max_chars: int = _MAX_SEGMENT_CHARS) -> bool:
    return any(len(s) > max_chars for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()))


def _split_for_tts(text: str, max_chars: int = _MAX_SEGMENT_CHARS) -> list[str]:
    """Segments no longer than max_chars, breaking at sentence ends, then clauses, then words.
    Punctuation is kept so Piper still produces natural pauses."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    segments: list[str] = []
    for sent in re.findall(r"[^.!?]*[.!?]+|\S[^.!?]*$", text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= max_chars:
            segments.append(sent)
            continue
        cur = ""
        for clause in re.findall(r"[^,;:]*[,;:]+|\S[^,;:]*$", sent):
            clause = clause.strip()
            if not clause:
                continue
            if len(cur) + len(clause) + 1 <= max_chars:
                cur = (cur + " " + clause).strip()
                continue
            if cur:
                segments.append(cur)
            if len(clause) <= max_chars:
                cur = clause
            else:  # one clause longer than the whole budget -> split on words
                piece = ""
                for word in clause.split():
                    if len(piece) + len(word) + 1 <= max_chars:
                        piece = (piece + " " + word).strip()
                    else:
                        if piece:
                            segments.append(piece)
                        piece = word
                cur = piece
        if cur:
            segments.append(cur)
    return segments


def synthesize(text: str) -> tuple[bytes, int]:
    """Text -> (wav_bytes, sample_rate). Synchronous: call from a threadpool.

    Written defensively across piper versions: 1.3+ exposes synthesize_wav, ~1.2 streams raw PCM,
    and older builds write into a wave object. Whichever exists is used.
    """
    import wave

    text = (text or "").strip()
    if not text:
        raise RuntimeError("Empty text for synthesis.")
    tts = _load_tts()
    sr = int(getattr(getattr(tts, "config", None), "sample_rate", 22050))
    buf = io.BytesIO()

    if _needs_segmentation(text) and hasattr(tts, "synthesize"):
        try:
            pcm = bytearray()
            seg_sr, width, channels = sr, 2, 1
            got = False
            for seg in _split_for_tts(text):
                for chunk in tts.synthesize(seg):
                    data = getattr(chunk, "audio_int16_bytes", None)
                    if data:
                        pcm.extend(data)
                        got = True
                        seg_sr = int(getattr(chunk, "sample_rate", seg_sr))
                        width = int(getattr(chunk, "sample_width", width))
                        channels = int(getattr(chunk, "sample_channels", channels))
            if got:
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(width)
                    wf.setframerate(seg_sr)
                    wf.writeframes(bytes(pcm))
                return buf.getvalue(), seg_sr
        except Exception:  # noqa: BLE001 - never fail the reply over a formatting optimisation
            log.warning("voice: segmented synthesis failed - using the whole-text path", exc_info=True)
            buf = io.BytesIO()

    if hasattr(tts, "synthesize_wav"):
        with wave.open(buf, "wb") as wf:
            tts.synthesize_wav(text, wf)
        return buf.getvalue(), sr

    if hasattr(tts, "synthesize_stream_raw"):
        pcm = bytearray()
        for chunk in tts.synthesize_stream_raw(text):
            pcm.extend(chunk)
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(bytes(pcm))
        return buf.getvalue(), sr

    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        tts.synthesize(text, wf)
    return buf.getvalue(), sr
