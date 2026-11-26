import { useCallback, useEffect, useRef, useState } from "react";

import { androidShell } from "./android-shell";
import { synthesizeTts } from "./companion-api";
import { spokenText } from "./fileKinds";

export type SpeechPlayerStatus = "idle" | "loading" | "playing";

export interface SpeechPlayerState {
  status: SpeechPlayerStatus;
  activeMessageId: string | null;
  previewText: string;
  isFallback: boolean;
  play: (text: string, messageId?: string) => Promise<void>;
  toggle: (text: string, messageId?: string) => Promise<void>;
  stop: () => void;
}

/**
 * Neural TTS speech player with automatic fallback to native Android TTS or Web Speech API.
 * Prefers server-cached MP3 audio when ``ttsEnabled`` is true.
 */
export function useSpeechPlayer({
  token,
  ttsEnabled,
}: {
  token: string;
  ttsEnabled: boolean;
}): SpeechPlayerState {
  const [status, setStatus] = useState<SpeechPlayerStatus>("idle");
  const [activeMessageId, setActiveMessageId] = useState<string | null>(null);
  const [previewText, setPreviewText] = useState("");
  const [isFallback, setIsFallback] = useState(false);

  const seqRef = useRef(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const androidPollRef = useRef<number | null>(null);

  const clearPlaybackResources = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    if (androidPollRef.current !== null) {
      window.clearInterval(androidPollRef.current);
      androidPollRef.current = null;
    }
    const audio = audioRef.current;
    if (audio) {
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
      try {
        audio.src = "";
      } catch {
        // Ignore read-only src mocks in tests.
      }
      audioRef.current = null;
    }
    androidShell()?.stopSpeaking?.();
    if (typeof window !== "undefined") {
      window.speechSynthesis?.cancel?.();
    }
  }, []);

  const stop = useCallback(() => {
    seqRef.current += 1;
    clearPlaybackResources();
    setStatus("idle");
    setActiveMessageId(null);
    setPreviewText("");
    setIsFallback(false);
  }, [clearPlaybackResources]);

  const speakFallback = useCallback((words: string, seq: number) => {
    if (seq !== seqRef.current) return;
    const lang = /[\u4e00-\u9fff]/.test(words) ? "zh-CN" : "en-US";
    const android = androidShell();
    if (android?.speak) {
      setIsFallback(true);
      setStatus("playing");
      android.speak(words, lang);
      if (android.speaking) {
        const startedAt = Date.now();
        androidPollRef.current = window.setInterval(() => {
          if (seq !== seqRef.current) return;
          if (Date.now() - startedAt < 800) return;
          if (!androidShell()?.speaking?.()) {
            stop();
          }
        }, 500);
      }
      return;
    }
    const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;
    if (synth && typeof SpeechSynthesisUtterance !== "undefined") {
      const utterance = new SpeechSynthesisUtterance(words);
      utterance.lang = lang;
      utterance.onend = () => {
        if (seq === seqRef.current) stop();
      };
      utterance.onerror = () => {
        if (seq === seqRef.current) stop();
      };
      setIsFallback(true);
      setStatus("playing");
      synth.speak(utterance);
      return;
    }
    stop();
  }, [stop]);

  const play = useCallback(async (text: string, messageId?: string) => {
    const words = spokenText(text).trim();
    if (!words) return;

    const seq = ++seqRef.current;
    clearPlaybackResources();
    setActiveMessageId(messageId ?? null);
    setPreviewText(words.slice(0, 28));
    setIsFallback(false);

    if (!ttsEnabled || !token) {
      speakFallback(words, seq);
      return;
    }

    const controller = new AbortController();
    abortRef.current = controller;
    setStatus("loading");

    try {
      const result = await synthesizeTts(token, words, { signal: controller.signal });
      if (seq !== seqRef.current) return;
      if (!result.ok || !result.url || typeof Audio === "undefined") {
        speakFallback(words, seq);
        return;
      }

      const audio = new Audio(result.url);
      audioRef.current = audio;
      let settled = false;
      audio.onended = () => {
        if (seq === seqRef.current) stop();
      };
      audio.onerror = () => {
        if (settled || seq !== seqRef.current) return;
        settled = true;
        audioRef.current = null;
        speakFallback(words, seq);
      };

      await Promise.resolve(audio.play());
      if (seq !== seqRef.current) {
        audio.pause();
        return;
      }
      if (!settled) {
        settled = true;
        setStatus("playing");
      }
    } catch (error) {
      if (seq !== seqRef.current) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      speakFallback(words, seq);
    }
  }, [clearPlaybackResources, speakFallback, stop, token, ttsEnabled]);

  const toggle = useCallback(async (text: string, messageId?: string) => {
    if (status !== "idle" && (!messageId || activeMessageId === messageId)) {
      stop();
      return;
    }
    await play(text, messageId);
  }, [activeMessageId, play, status, stop]);

  useEffect(() => () => {
    clearPlaybackResources();
  }, [clearPlaybackResources]);

  return {
    status,
    activeMessageId,
    previewText,
    isFallback,
    play,
    toggle,
    stop,
  };
}
