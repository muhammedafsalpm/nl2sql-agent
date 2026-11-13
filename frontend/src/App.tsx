import { useEffect, useRef, useState } from "react";
import { fetchInfo, streamChat } from "./api/client";
import { Composer } from "./components/Composer";
import { MessageBubble } from "./components/MessageBubble";
import type { InfoResponse, Message, Mode, Turn } from "./types";
import { useSpeaker } from "./voice/useSpeaker";
import { useVoiceRecorder } from "./voice/useVoiceRecorder";

/**
 * Starter prompts, built from the entities the connected database actually contains
 * (`/api/info` reports them). Nothing here names a specific table or question, so the UI needs no
 * edit when the database changes — previously these were four hardcoded questions that would be
 * meaningless against any other schema.
 */
function suggestionsFor(info: InfoResponse | null): string[] {
  const generic = ["What data do you have?", "Show me a chart of the largest categories"];
  if (!info?.entities?.length) return generic;
  const [first, second] = info.entities;
  const out = [`How many ${first} are there?`];
  if (second) {
    out.push(`Show a donut chart of ${first} per ${second}`);
    out.push(`List 5 ${second}`);
  }
  out.push(`What data do you have about ${first}?`);
  return out;
}

/**
 * How many past exchanges to replay to the API.
 *
 * The server caps this again at HISTORY_TURNS (default 3) and stays the authority; a few
 * extra are sent so raising that setting needs no frontend rebuild. Without a slice the
 * ENTIRE session was posted on every request, so a long conversation grew the payload
 * without limit - the server then discarded all but the last few turns anyway.
 */
const HISTORY_TURNS_SENT = 10;

/** Shown until /api/info answers, and if the backend predates the app_name field. */
const DEFAULT_APP_NAME = "SQL Assistant";

export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  // Defaults to visible in both modes (Fast and Deep Search share this one toggle -
  // there is no per-mode setting, so this default applies identically to both).
  const [includeSql, setIncludeSql] = useState(true);
  // Fast by default. Deep Search is opt-in and never switched on automatically.
  const [deepSearch, setDeepSearch] = useState(false);
  const [info, setInfo] = useState<InfoResponse | null>(null);
  const abort = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const seq = useRef(0);

  // Voice (optional). Both hooks are inert unless the backend reports voice as available: the
  // recorder only runs when the mic is pressed, and the speaker only when a spoken question was
  // asked. Neither is on the typed-question path.
  const speaker = useSpeaker();
  // speakReply=true here is what makes a spoken question get a spoken answer.
  const recorder = useVoiceRecorder((text) => void send(text, undefined, true));

  useEffect(() => {
    fetchInfo().then(setInfo).catch(() => setInfo(null));
  }, []);

  // The name comes from APP_NAME in backend/.env, so renaming the assistant needs no rebuild.
  const appName = info?.app_name || DEFAULT_APP_NAME;
  useEffect(() => {
    document.title = appName;
  }, [appName]);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  /** The API is stateless — the client owns the conversation and replays it every turn. */
  function historyFrom(list: Message[]): Turn[] {
    const turns: Turn[] = [];
    for (let i = 0; i < list.length - 1; i++) {
      if (list[i].role === "user" && list[i + 1].role === "assistant" && !list[i + 1].error) {
        turns.push({ user: list[i].text, assistant: list[i + 1].text });
      }
    }
    return turns.slice(-HISTORY_TURNS_SENT);
  }

  function patch(id: string, changes: Partial<Message>) {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...changes } : m)));
  }

  /**
   * `forceMode` lets an answer be re-run on the thorough path without flipping the toggle.
   * `speakReply` is true ONLY when the question arrived by voice, so a typed question stays
   * silent. The pipeline itself is identical either way — voice only supplies the text and
   * optionally reads the finished answer back.
   */
  async function send(question: string, forceMode?: Mode, speakReply = false) {
    const mode: Mode = forceMode ?? (deepSearch ? "deep" : "normal");
    const stamp = ++seq.current;
    const userMsg: Message = { id: `u${stamp}`, role: "user", text: question };
    const replyId = `a${stamp}`;
    const reply: Message = {
      id: replyId,
      role: "assistant",
      text: "",
      pending: mode === "deep" ? "Deep Search — checking carefully…" : "Thinking…",
      sql: [],
      mode,
    };

    const history = historyFrom(messages);
    setMessages((prev) => [...prev, userMsg, reply]);
    setBusy(true);
    abort.current = new AbortController();

    await streamChat(
      { question, history, include_sql: includeSql, mode },
      {
        // The backend reports the real step it just finished. A heartbeat carries no stage, so
        // the last real one is kept and only the elapsed time ticks up.
        onStatus: (stage, seconds) =>
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id !== replyId) return m;
              const label = stage ?? m.stage ?? "Working";
              const secs = seconds ? ` · ${seconds}s` : "";
              return { ...m, stage: label, pending: `${label}${secs}` };
            }),
          ),
        onSql: (sql) =>
          setMessages((prev) =>
            prev.map((m) => (m.id === replyId ? { ...m, sql: [...(m.sql ?? []), sql] } : m)),
          ),
        onChart: (chart) => patch(replyId, { chart }),
        onDone: (payload) => {
          patch(replyId, {
            text: payload.answer || "(no answer produced)",
            route: payload.route,
            verified: payload.verified ?? true,
            latency: payload.latency_seconds,
            chart: payload.chart ?? undefined,
            // Null on chitchat/clarify, which is precisely when there is nothing to export.
            exportToken: payload.export_token ?? null,
            pending: undefined,
          });
          // Read the answer back only for a spoken question. speak() is fire-and-forget and
          // swallows its own errors, so a TTS problem can never affect the answer on screen.
          if (speakReply && payload.answer) void speaker.speak(payload.answer);
        },
        onError: (message) =>
          patch(replyId, { text: message, error: true, pending: undefined }),
      },
      abort.current.signal,
    );

    setBusy(false);
    abort.current = null;
  }

  function stop() {
    abort.current?.abort();
    setBusy(false);
    setMessages((prev) =>
      prev.map((m) =>
        m.pending ? { ...m, pending: undefined, text: "Stopped.", error: true } : m,
      ),
    );
  }

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1>{appName}</h1>
          <p className="subtitle">
            {info
              ? `${info.database} · ${info.model}${info.fast_model_enabled ? ` + ${info.fast_model}` : ""} · read-only`
              : "connecting to the backend…"}
          </p>
        </div>
        {/* Mode and SQL controls live on the composer, next to where you type. */}
      </header>

      <div className="transcript" ref={scroller}>
        {messages.length === 0 ? (
          <div className="empty">
            <h2>Ask a question about your data.</h2>
            {info?.app_description && <p className="empty-desc">{info.app_description}</p>}
            <p>Every query is read-only. Ask for a chart and you'll get one.</p>
            <div className="suggestions">
              {suggestionsFor(info).map((s) => (
                <button key={s} className="chip" onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m, i) => (
            <MessageBubble
              key={m.id}
              message={m}
              // The question this answer came from — used for the export file name and title.
              // Resolved the same way onDeepSearch does: the user turn immediately before.
              question={
                m.role === "assistant" && messages[i - 1]?.role === "user"
                  ? messages[i - 1].text
                  : ""
              }
              // Offer a thorough re-run of the question that produced this answer. The user
              // asks for it — the system never switches mode on its own.
              onDeepSearch={
                m.role === "assistant" && m.mode === "normal" && !m.pending && !busy
                  ? () => {
                    const asked = messages[i - 1];
                    if (asked?.role === "user") send(asked.text, "deep");
                  }
                  : undefined
              }
            />
          ))
        )}
      </div>

      <p className="disclaimer">
        {appName} can make mistakes. Check important info.
      </p>

      <Composer
        busy={busy}
        onSend={send}
        onStop={stop}
        deepSearch={deepSearch}
        onToggleDeepSearch={() => setDeepSearch((v) => !v)}
        showSql={includeSql}
        onToggleShowSql={() => setIncludeSql((v) => !v)}
        recorder={recorder}
        speaking={speaker.speaking}
        onStopSpeaking={speaker.stop}
      />
    </div>
  );
}
