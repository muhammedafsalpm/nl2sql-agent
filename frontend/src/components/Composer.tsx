import { useRef, useState } from "react";
import { MicButton } from "../voice/MicButton";
import type { VoiceRecorder } from "../voice/useVoiceRecorder";

interface Props {
  busy: boolean;
  onSend: (question: string) => void;
  onStop: () => void;
  deepSearch: boolean;
  onToggleDeepSearch: () => void;
  showSql: boolean;
  onToggleShowSql: () => void;
  /** Voice controls. MicButton renders nothing when the backend has no voice, so the composer
   *  looks exactly as it did before this feature when voice is not installed. */
  recorder: VoiceRecorder;
  speaking: boolean;
  onStopSpeaking: () => void;
}

/** Pill toggle that sits on the composer, beside where you type. */
function Pill({
  active,
  onClick,
  icon,
  label,
  title,
}: {
  active: boolean;
  onClick: () => void;
  icon: string;
  label: string;
  title: string;
}) {
  return (
    <button
      type="button"
      className={`pill${active ? " pill-on" : ""}`}
      onClick={onClick}
      title={title}
      aria-pressed={active}
    >
      <span className="pill-icon" aria-hidden="true">
        {icon}
      </span>
      {label}
    </button>
  );
}

export function Composer({
  busy,
  onSend,
  onStop,
  deepSearch,
  onToggleDeepSearch,
  showSql,
  onToggleShowSql,
  recorder,
  speaking,
  onStopSpeaking,
}: Props) {
  const [value, setValue] = useState("");
  const box = useRef<HTMLTextAreaElement>(null);

  function submit() {
    const question = value.trim();
    if (!question || busy) return;
    onSend(question);
    setValue("");
    box.current?.focus();
  }

  return (
    <div className="composer">
      <textarea
        ref={box}
        rows={1}
        value={value}
        placeholder="Ask a question about your data… or ask for a chart"
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
      />

      <div className="composer-bar">
        <div className="composer-pills">
          <Pill
            active={deepSearch}
            onClick={onToggleDeepSearch}
            icon="◈"
            label="Deep Search"
            title="Run every check — slower, but the numbers are independently verified"
          />
          <Pill
            active={showSql}
            onClick={onToggleShowSql}
            icon="⌘"
            label="Show SQL"
            title="Show the query behind each answer"
          />
        </div>

        {/* Sits just before the send button, so mic and send read as one pair. */}
        <MicButton
          recorder={recorder}
          speaking={speaking}
          onStopSpeaking={onStopSpeaking}
          busy={busy}
        />

        {busy ? (
          <button className="send send-stop" onClick={onStop} title="Stop">
            ■
          </button>
        ) : (
          <button
            className="send"
            onClick={submit}
            disabled={!value.trim()}
            title="Send"
            aria-label="Send"
          >
            ↑
          </button>
        )}
      </div>
    </div>
  );
}
