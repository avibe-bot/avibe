import { avibeFetch } from './avibeFetch';

export const VOICE_CONTEXT_BEFORE_CHARS = 500;
export const VOICE_CONTEXT_AFTER_CHARS = 200;
export const VOICE_CLEANUP_TIMEOUT_MS = 35_000;

export type VoiceInsertionSnapshot = {
  text: string;
  start: number;
  end: number;
  before: string;
  after: string;
};

type VoiceCleanupFetch = (
  path: string,
  init?: RequestInit,
) => Promise<Response>;

type VoiceCleanupDependencies = {
  cloudFetch?: VoiceCleanupFetch;
  timeoutMs?: number;
  signal?: AbortSignal;
};

const boundedSelection = (text: string, start: number, end: number): [number, number] => {
  const safeStart = Math.max(0, Math.min(text.length, Math.floor(start)));
  const safeEnd = Math.max(safeStart, Math.min(text.length, Math.floor(end)));
  return [safeStart, safeEnd];
};

export const voiceInsertionSnapshot = (
  text: string,
  start: number,
  end: number,
): VoiceInsertionSnapshot => {
  const [safeStart, safeEnd] = boundedSelection(text, start, end);
  return {
    text,
    start: safeStart,
    end: safeEnd,
    before: text.slice(Math.max(0, safeStart - VOICE_CONTEXT_BEFORE_CHARS), safeStart),
    after: text.slice(safeEnd, safeEnd + VOICE_CONTEXT_AFTER_CHARS),
  };
};

const WORD_CHARACTER = /[\p{L}\p{N}_]/u;
const NO_SPACE_SCRIPT = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;

const needsBoundarySpace = (left: string, right: string): boolean => {
  const leftChar = left.at(-1) ?? '';
  const rightChar = right.at(0) ?? '';
  return (
    WORD_CHARACTER.test(leftChar)
    && WORD_CHARACTER.test(rightChar)
    && !NO_SPACE_SCRIPT.test(leftChar)
    && !NO_SPACE_SCRIPT.test(rightChar)
  );
};

export const voiceInsertionText = (
  currentText: string,
  snapshot: VoiceInsertionSnapshot,
  transcript: string,
): string | null => {
  if (currentText !== snapshot.text) return null;
  const normalized = transcript.trim();
  if (!normalized) return '';
  const left = currentText.slice(0, snapshot.start);
  const right = currentText.slice(snapshot.end);
  return `${needsBoundarySpace(left, normalized) ? ' ' : ''}${normalized}${
    needsBoundarySpace(normalized, right) ? ' ' : ''
  }`;
};

export const applyVoiceInsertion = (
  currentText: string,
  snapshot: VoiceInsertionSnapshot,
  transcript: string,
): string | null => {
  const insertion = voiceInsertionText(currentText, snapshot, transcript);
  if (insertion === null) return null;
  return `${currentText.slice(0, snapshot.start)}${insertion}${currentText.slice(snapshot.end)}`;
};

const timeoutSignal = (durationMs: number, externalSignal?: AbortSignal) => {
  const controller = new AbortController();
  const abortFromExternal = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) {
    abortFromExternal();
  } else {
    externalSignal?.addEventListener('abort', abortFromExternal, { once: true });
  }
  const timer = globalThis.setTimeout(
    () => controller.abort(new DOMException('voice cleanup timed out', 'TimeoutError')),
    durationMs,
  );
  return {
    signal: controller.signal,
    cancel: () => {
      globalThis.clearTimeout(timer);
      externalSignal?.removeEventListener('abort', abortFromExternal);
    },
  };
};

// Cleanup is deliberately best-effort. Raw ASR text remains usable when an old
// cloud deployment lacks the endpoint or the small editing model is unavailable.
export const cleanupVoiceTranscript = async (
  transcript: string,
  snapshot: VoiceInsertionSnapshot,
  dependencies: VoiceCleanupDependencies = {},
): Promise<string> => {
  const request = timeoutSignal(
    dependencies.timeoutMs ?? VOICE_CLEANUP_TIMEOUT_MS,
    dependencies.signal,
  );
  try {
    const response = await (dependencies.cloudFetch ?? avibeFetch)(
      '/api/cloud/voice/cleanup',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          transcript,
          before: snapshot.before,
          after: snapshot.after,
        }),
        signal: request.signal,
      },
    );
    if (!response.ok) return transcript;
    const payload = await response.json().catch(() => null) as { text?: unknown } | null;
    return typeof payload?.text === 'string' ? payload.text : transcript;
  } catch {
    return transcript;
  } finally {
    request.cancel();
  }
};
