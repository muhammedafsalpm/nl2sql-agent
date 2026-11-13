/**
 * Voice API client.
 *
 * Uses `fetch` to match the rest of this app (src/api/client.ts) — there is no axios here.
 * The API key header mirrors that file exactly, so voice inherits the app's auth convention
 * rather than inventing a second one.
 */
const API_KEY = import.meta.env.VITE_API_KEY as string | undefined;

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = { ...extra };
  if (API_KEY) h["X-API-Key"] = API_KEY;
  return h;
}

export interface VoiceStatus {
  available: boolean;
  reason?: string;
  stt_model?: string;
  tts_model?: string;
  device?: string;
  lang?: string;
}

/** Whether the backend can do voice. Never loads a model, so it is safe to call on page load. */
export async function getVoiceStatus(): Promise<VoiceStatus> {
  const res = await fetch("/api/voice/status", { headers: authHeaders() });
  if (!res.ok) return { available: false, reason: `status ${res.status}` };
  return res.json();
}

/**
 * Speech -> text.
 *
 * The recording is sent as the RAW request body, not multipart/form-data: the backend reads
 * request.body() so it does not need the `python-multipart` package, whose absence would
 * otherwise stop the whole API from starting. Content-Type tells the server the container
 * format, which varies by browser (Chrome/Edge produce webm, Safari mp4).
 */
export async function transcribe(audio: Blob): Promise<string> {
  const res = await fetch("/api/voice/transcribe", {
    method: "POST",
    headers: authHeaders({ "Content-Type": audio.type || "audio/webm" }),
    body: audio,
  });
  if (!res.ok) throw new Error(await readError(res, "Could not transcribe the recording."));
  const data = (await res.json()) as { text?: string };
  return (data.text ?? "").trim();
}

/** Text -> speech. Returns a WAV blob the browser can play directly. */
export async function synthesize(text: string): Promise<Blob> {
  const res = await fetch("/api/voice/synthesize", {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ text }),
  });
  if (!res.ok) throw new Error(await readError(res, "Could not generate speech."));
  return res.blob();
}

/** FastAPI puts the readable reason in `detail`; fall back to the status when it is not JSON. */
async function readError(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    if (body?.detail) return String(body.detail);
  } catch {
    /* not JSON - use the fallback below */
  }
  return `${fallback} (${res.status})`;
}
