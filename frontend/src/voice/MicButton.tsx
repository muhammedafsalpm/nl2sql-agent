import { useEffect, useState, type ReactNode } from "react";
import { getVoiceStatus } from "./voiceApi";
import { VOICE_BAR_COUNT, type VoiceRecorder } from "./useVoiceRecorder";
import "./voice.css";

interface Props {
  recorder: VoiceRecorder;
  /** True while an answer is being spoken — shows the stop-speaking control instead. */
  speaking: boolean;
  onStopSpeaking: () => void;
  /** The chat is busy answering; recording is pointless until it finishes. */
  busy: boolean;
}

/**
 * Inline SVG icons rather than emoji.
 *
 * Emoji render as full-colour glyphs from the OS font — they look different on every platform,
 * ignore the theme, and sit inconsistently on the baseline next to the composer's other controls.
 * These use `currentColor`, so each one inherits the button's colour and matches the app's dark
 * theme, and they stay pixel-identical everywhere.
 */
function Icon({ path, filled = false }: { path: ReactNode; filled?: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="17"
      height="17"
      fill={filled ? "currentColor" : "none"}
      stroke={filled ? "none" : "currentColor"}
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {path}
    </svg>
  );
}

const MicIcon = () => (
  <Icon
    path={
      <>
        <rect x="9" y="2" width="6" height="11" rx="3" />
        <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
        <line x1="12" y1="19" x2="12" y2="22" />
      </>
    }
  />
);

const StopIcon = () => <Icon filled path={<rect x="7" y="7" width="10" height="10" rx="2" />} />;

const CheckIcon = () => <Icon path={<polyline points="20 6 9 17 4 12" />} />;

const CloseIcon = () => (
  <Icon
    path={
      <>
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </>
    }
  />
);

const AlertIcon = () => (
  <Icon
    path={
      <>
        <circle cx="12" cy="12" r="9" />
        <line x1="12" y1="8" x2="12" y2="13" />
        <line x1="12" y1="16.5" x2="12" y2="16.5" />
      </>
    }
  />
);

/**
 * Mic control for the composer.
 *
 * Renders NOTHING until /api/voice/status says voice is available, so a backend without the
 * voice packages installed shows exactly the UI it showed before this feature existed.
 */
export function MicButton({ recorder, speaking, onStopSpeaking, busy }: Props) {
  const [available, setAvailable] = useState(false);
  const { recording, transcribing, error, seconds, levels, start, stop, cancel, clearError } = recorder;

  useEffect(() => {
    let cancelled = false;
    getVoiceStatus()
      .then((s) => {
        if (!cancelled) setAvailable(!!s.available);
      })
      .catch(() => {
        if (!cancelled) setAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!available) return null;

  if (error) {
    return (
      <button
        type="button"
        className="voice-btn voice-error"
        onClick={clearError}
        title={error}
        aria-label={error}
      >
        <AlertIcon />
      </button>
    );
  }

  if (speaking) {
    return (
      <button
        type="button"
        className="voice-btn voice-speaking"
        onClick={onStopSpeaking}
        title="Stop speaking"
        aria-label="Stop speaking"
      >
        <StopIcon />
      </button>
    );
  }

  if (transcribing) {
    return (
      <span className="voice-btn voice-thinking" title="Transcribing…" aria-live="polite">
        <span className="voice-spinner" aria-hidden="true" />
      </span>
    );
  }

  if (recording) {
    return (
      <span className="voice-recording" role="status" aria-live="polite">
        <button
          type="button"
          className="voice-btn voice-cancel"
          onClick={cancel}
          title="Discard"
          aria-label="Discard recording"
        >
          <CloseIcon />
        </button>
        <span className="voice-bars" aria-hidden="true">
          {Array.from({ length: VOICE_BAR_COUNT }, (_, i) => (
            <span key={i} className="voice-bar" style={{ transform: `scaleY(${levels[i] ?? 0.08})` }} />
          ))}
        </span>
        <span className="voice-timer">
          {Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}
        </span>
        <button
          type="button"
          className="voice-btn voice-send"
          onClick={stop}
          title="Send recording"
          aria-label="Send recording"
        >
          <CheckIcon />
        </button>
      </span>
    );
  }

  return (
    <button
      type="button"
      className="voice-btn voice-mic"
      onClick={start}
      disabled={busy}
      title={busy ? "Wait for the current answer" : "Ask by voice"}
      aria-label="Ask by voice"
    >
      <MicIcon />
    </button>
  );
}
