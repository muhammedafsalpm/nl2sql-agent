import { useEffect, useRef, useState } from "react";

import { downloadExport } from "../api/client";
import type { ExportFormat } from "../types";

/**
 * "Export" control shown under an answer that returned data.
 *
 * Rendered only when the turn produced an export token, so chitchat and clarify replies never
 * show it — no route check needed here, the token's presence IS the condition.
 *
 * The downloaded file always contains the COMPLETE result, not the capped preview shown in the
 * chat: MAX_ROWS limits what reaches the model, and no model is involved in an export.
 */
const OPTIONS: { format: ExportFormat; label: string; hint: string }[] = [
  { format: "xlsx", label: "Excel", hint: "Spreadsheet (.xlsx) — every row" },
  { format: "docx", label: "Word", hint: "Document (.docx) — answer + table" },
];

export function ExportMenu({
  token,
  question,
  answer,
  verified,
}: {
  token: string;
  question: string;
  answer: string;
  verified: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const [error, setError] = useState("");
  const wrapRef = useRef<HTMLDivElement>(null);

  // Close on an outside click or Escape, so the menu never strands itself open over the chat.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function pick(format: ExportFormat) {
    setBusy(format);
    setError("");
    try {
      await downloadExport({ token, format, question, answer, verified });
      setOpen(false);
    } catch (err) {
      // Keep the menu open so the message is visible next to what was clicked.
      setError((err as Error).message || "Export failed.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="export-menu" ref={wrapRef}>
      <button
        className="export-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
        title="Download the full data behind this answer"
      >
        {open ? "▾" : "▸"} Export
      </button>

      {open && (
        <div className="export-options" role="menu">
          {OPTIONS.map((o) => (
            <button
              key={o.format}
              className="export-option"
              role="menuitem"
              disabled={busy !== null}
              onClick={() => pick(o.format)}
              title={o.hint}
            >
              <span className="export-option-label">{o.label}</span>
              <span className="export-option-hint">
                {busy === o.format ? "preparing…" : o.hint}
              </span>
            </button>
          ))}
          {error && <div className="export-error">{error}</div>}
        </div>
      )}
    </div>
  );
}
