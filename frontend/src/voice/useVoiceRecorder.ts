import { useCallback, useEffect, useRef, useState } from "react";
import { transcribe } from "./voiceApi";

export const VOICE_BAR_COUNT = 5;
const IDLE_LEVEL = 0.08;

export interface VoiceRecorder {
  recording: boolean;
  transcribing: boolean;
  error: string;
  seconds: number;
  /** Live mic amplitude per bar, 0-1. Real audio, not a decorative animation. */
  levels: number[];
  start: () => Promise<void>;
  stop: () => void;
  cancel: () => void;
  clearError: () => void;
}

/**
 * Microphone recording + transcription.
 *
 * Kept as a hook in the voice folder rather than inside App.tsx so the chat component stays
 * unaware of MediaRecorder, AudioContext and their teardown rules. `onText` receives the
 * transcript; the caller decides what to do with it (here: send it as a question).
 */
export function useVoiceRecorder(onText: (text: string) => void): VoiceRecorder {
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [error, setError] = useState("");
  const [seconds, setSeconds] = useState(0);
  const [levels, setLevels] = useState<number[]>(() => Array(VOICE_BAR_COUNT).fill(IDLE_LEVEL));

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const rafRef = useRef<number | null>(null);
  const timerRef = useRef<number | null>(null);
  const cancelledRef = useRef(false);
  // onText is read inside a MediaRecorder callback that is created once; a ref keeps that
  // callback pointing at the latest closure instead of the one captured at start().
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  const teardown = useCallback(() => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    timerRef.current = null;
    audioCtxRef.current?.close().catch(() => {});
    audioCtxRef.current = null;
    // Releasing every track is what turns the browser's recording indicator off.
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    recorderRef.current = null;
    setLevels(Array(VOICE_BAR_COUNT).fill(IDLE_LEVEL));
    setSeconds(0);
  }, []);

  // Stop the microphone if the component unmounts mid-recording.
  useEffect(() => teardown, [teardown]);

  const meter = useCallback((stream: MediaStream) => {
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ctx = new Ctx();
    audioCtxRef.current = ctx;
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    const buf = new Uint8Array(analyser.frequencyBinCount);

    const tick = () => {
      analyser.getByteTimeDomainData(buf);
      // RMS around the 128 midpoint = actual loudness, so the bars track the user's voice.
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buf.length);
      const next = Array.from({ length: VOICE_BAR_COUNT }, (_, i) => {
        const jitter = 0.75 + 0.5 * Math.sin(Date.now() / 120 + i);
        return Math.max(IDLE_LEVEL, Math.min(1, rms * 3.2 * jitter));
      });
      setLevels(next);
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
  }, []);

  const handleStop = useCallback(async () => {
    const chunks = chunksRef.current;
    chunksRef.current = [];
    const type = recorderRef.current?.mimeType || "audio/webm";
    teardown();
    setRecording(false);

    if (cancelledRef.current || chunks.length === 0) return;

    const blob = new Blob(chunks, { type });
    if (blob.size < 1200) {
      setError("That recording was too short — press and hold a moment longer.");
      return;
    }

    setTranscribing(true);
    try {
      const text = await transcribe(blob);
      if (!text) {
        setError("Didn't catch that — please try again.");
        return;
      }
      onTextRef.current(text);
    } catch (err) {
      setError((err as Error).message || "Transcription failed.");
    } finally {
      setTranscribing(false);
    }
  }, [teardown]);

  const start = useCallback(async () => {
    if (recording || transcribing) return;
    setError("");
    // getUserMedia only exists in a secure context: https, or http on localhost. Over plain http
    // on a LAN address the browser removes it entirely, so say that rather than "unknown error".
    if (!navigator.mediaDevices?.getUserMedia) {
      setError(
        window.isSecureContext
          ? "This browser does not support microphone recording."
          : "The microphone needs a secure page — open the app on localhost or over https.",
      );
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      cancelledRef.current = false;
      chunksRef.current = [];

      const rec = new MediaRecorder(stream);
      rec.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      rec.onstop = handleStop;
      recorderRef.current = rec;
      rec.start();

      meter(stream);
      setRecording(true);
      setSeconds(0);
      timerRef.current = window.setInterval(() => setSeconds((s) => s + 1), 1000);
    } catch (err) {
      teardown();
      const name = (err as Error).name;
      setError(
        name === "NotAllowedError"
          ? "Microphone permission was blocked. Allow it in your browser settings."
          : name === "NotFoundError"
            ? "No microphone was found."
            : `Could not start recording: ${(err as Error).message}`,
      );
    }
  }, [recording, transcribing, handleStop, meter, teardown]);

  const stop = useCallback(() => {
    cancelledRef.current = false;
    recorderRef.current?.state === "recording" ? recorderRef.current.stop() : teardown();
  }, [teardown]);

  const cancel = useCallback(() => {
    cancelledRef.current = true;
    if (recorderRef.current?.state === "recording") recorderRef.current.stop();
    else teardown();
    setRecording(false);
  }, [teardown]);

  return {
    recording,
    transcribing,
    error,
    seconds,
    levels,
    start,
    stop,
    cancel,
    clearError: () => setError(""),
  };
}
