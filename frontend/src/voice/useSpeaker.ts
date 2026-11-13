import { useCallback, useEffect, useRef, useState } from "react";
import { synthesize } from "./voiceApi";

export interface Speaker {
  speaking: boolean;
  /** Fetch the audio for this text and play it, replacing anything already playing. */
  speak: (text: string) => Promise<void>;
  stop: () => void;
}

/**
 * Speaks an answer aloud.
 *
 * Answers from this pipeline can be long ("Showing the first 20 of 224 wells…"), so stopping has
 * to be possible at any point — `stop()` is wired to a visible button in MicButton.
 */
export function useSpeaker(): Speaker {
  const [speaking, setSpeaking] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  // Bumped on every stop()/speak(); a reply that finishes downloading after the user moved on
  // compares this and drops itself instead of talking over the next answer.
  const runRef = useRef(0);

  const release = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.src = "";
      audioRef.current = null;
    }
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
  }, []);

  // Never leave audio playing after the component goes away.
  useEffect(() => () => release(), [release]);

  const stop = useCallback(() => {
    runRef.current += 1;
    release();
    setSpeaking(false);
  }, [release]);

  const speak = useCallback(
    async (text: string) => {
      const clean = (text || "").trim();
      if (!clean) return;

      runRef.current += 1;
      const run = runRef.current;
      release();
      setSpeaking(true);
      try {
        const blob = await synthesize(clean);
        if (run !== runRef.current) return; // superseded while the audio was being generated

        const url = URL.createObjectURL(blob);
        urlRef.current = url;
        const audio = new Audio(url);
        audioRef.current = audio;
        // Shared by both events, but assigned separately: onerror's handler type is
        // OnErrorEventHandler (its first argument can be a string), so it is not assignment-
        // compatible with an onended handler.
        const finish = () => {
          if (run === runRef.current) {
            release();
            setSpeaking(false);
          }
        };
        audio.onended = finish;
        audio.onerror = finish;
        await audio.play();
      } catch {
        // Speech is an enhancement: the answer is already on screen, so a TTS failure must never
        // surface as an error over a perfectly good reply.
        if (run === runRef.current) {
          release();
          setSpeaking(false);
        }
      }
    },
    [release],
  );

  return { speaking, speak, stop };
}
