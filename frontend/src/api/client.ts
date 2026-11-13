import type { ChartSpec, ChatRequest, ExportFormat, InfoResponse, Route } from "../types";

const API_KEY = import.meta.env.VITE_API_KEY as string | undefined;

function headers(): HeadersInit {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (API_KEY) h["X-API-Key"] = API_KEY;
  return h;
}

export async function fetchInfo(): Promise<InfoResponse> {
  const res = await fetch("/api/info", { headers: headers() });
  if (!res.ok) throw new Error(`/api/info returned ${res.status}`);
  return res.json();
}

/**
 * Download the full data behind an answer via POST /api/export.
 *
 * POST, not GET: the token embeds the query, and a GET would leave it in browser history and
 * server access logs. The response is a file, so it is read as a blob and handed to the browser
 * through a temporary object URL.
 */
export async function downloadExport(args: {
  token: string;
  format: ExportFormat;
  question: string;
  /** Answer text embedded as a preamble in docx; ignored for xlsx. */
  answer: string;
  verified: boolean;
}): Promise<void> {
  const res = await fetch("/api/export", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(args),
  });

  if (!res.ok) {
    // FastAPI puts the readable reason in `detail`; fall back to the status if it is not JSON.
    let detail = `Export failed (${res.status}).`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* keep the status-based message */
    }
    throw new Error(detail);
  }

  const blob = await res.blob();
  // Prefer the server's filename; it already encodes the question and the right extension.
  let filename = `export.${args.format}`;
  const disposition = res.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  if (match) filename = match[1];

  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke on the next tick: revoking synchronously can cancel the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export interface StreamHandlers {
  /** `stage` is absent on a plain heartbeat — keep whatever stage was last shown. */
  onStatus?: (stage: string | undefined, seconds?: number) => void;
  onSql?: (sql: string) => void;
  onChart?: (chart: ChartSpec) => void;
  onDone?: (payload: {
    answer: string;
    route: Route;
    verified?: boolean;
    mode?: string;
    latency_seconds: number;
    chart?: ChartSpec | null;
    /** Null when the turn returned no data — gates the Export control. */
    export_token?: string | null;
  }) => void;
  onError?: (message: string) => void;
}

/**
 * POST /api/chat/stream and dispatch Server-Sent Events as they arrive.
 *
 * EventSource cannot issue a POST, so the SSE framing is parsed by hand off the fetch body
 * stream. Events are separated by a blank line; each block carries `event:` and `data:` lines.
 */
export async function streamChat(
  req: ChatRequest,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify(req),
      signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    handlers.onError?.("Could not reach the API. Is the backend running on port 8000?");
    return;
  }

  if (!res.ok || !res.body) {
    if (res.status === 503) {
      handlers.onError?.("The backend is not ready — check the database connection.");
      return;
    }
    if (res.status === 401) {
      handlers.onError?.(
        "Authentication failed — the frontend's VITE_API_KEY doesn't match the backend's API_KEY.",
      );
      return;
    }
    // FastAPI puts the readable reason in `detail`; fall back to the raw text if it is not JSON.
    let detail = await res.text().catch(() => "");
    try {
      const body = JSON.parse(detail);
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* keep the raw text */
    }
    handlers.onError?.(`Request failed (${res.status}). ${detail.slice(0, 200)}`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split: number;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        dispatch(block, handlers);
      }
    }
  } catch (err) {
    if ((err as Error).name !== "AbortError") {
      handlers.onError?.("The connection dropped while the answer was being generated.");
    }
  }
}

function dispatch(block: string, handlers: StreamHandlers): void {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return;

  let payload: any;
  try {
    payload = JSON.parse(dataLines.join("\n"));
  } catch {
    return; // ignore a malformed frame rather than killing the stream
  }

  switch (event) {
    case "status":
      handlers.onStatus?.(payload.stage, payload.seconds);
      break;
    case "sql":
      handlers.onSql?.(payload.sql);
      break;
    case "chart":
      handlers.onChart?.(payload as ChartSpec);
      break;
    case "done":
      handlers.onDone?.(payload);
      break;
    case "error":
      handlers.onError?.(payload.message ?? "Unknown error");
      break;
  }
}
