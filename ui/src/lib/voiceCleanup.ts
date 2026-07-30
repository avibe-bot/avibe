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
  leftBoundary?: string;
  rightBoundary?: string;
};

type VoiceInsertionBoundaries = {
  left?: string;
  right?: string;
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

export type VoiceCleanupResult = {
  text: string;
  outcome: 'success' | 'fallback';
  elapsedMs: number;
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
  boundaries: VoiceInsertionBoundaries = {},
): VoiceInsertionSnapshot => {
  const [safeStart, safeEnd] = boundedSelection(text, start, end);
  return {
    text,
    start: safeStart,
    end: safeEnd,
    before: text.slice(Math.max(0, safeStart - VOICE_CONTEXT_BEFORE_CHARS), safeStart),
    after: text.slice(safeEnd, safeEnd + VOICE_CONTEXT_AFTER_CHARS),
    leftBoundary: boundaries.left,
    rightBoundary: boundaries.right,
  };
};

const WORD_CHARACTER = /[\p{L}\p{N}_]/u;
const NO_SPACE_SCRIPT = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Thai}\p{Script=Lao}\p{Script=Khmer}\p{Script=Myanmar}]/u;
const MENTION_AT_START = /^[@#]<([^>\n]+)>/u;
const MENTION_AT_END = /[@#]<([^>\n]+)>$/u;
const LEADING_SENTENCE_PUNCTUATION = /^[.,!?:;…，。！？：；、](?:\s|$)/u;
const LEADING_OUTER_BOUNDARY = /^[\p{P}\p{S}]+/u;
const TRAILING_OUTER_BOUNDARY = /[\p{P}\p{S}]+$/u;
const LEADING_WORD_GRAPHEME = /^[\p{L}\p{N}_]\p{M}*/u;
const TRAILING_WORD_GRAPHEME = /[\p{L}\p{N}_]\p{M}*$/u;
const OPENING_DELIMITER = /^[\p{Ps}\p{Pi}]$/u;
const SYMMETRIC_DELIMITER = /^["'`]$/u;
const TRAILING_TOKEN_JOINER = /(?:[/\\]|::|->|\?\.)$/u;
const LEADING_CALL_DELIMITER = /^[([{]/u;

const edgeCharacter = (text: string, side: 'start' | 'end'): string => {
  const wordGrapheme = side === 'start'
    ? text.match(LEADING_WORD_GRAPHEME)
    : text.match(TRAILING_WORD_GRAPHEME);
  if (wordGrapheme) return wordGrapheme[0];
  const characters = Array.from(text);
  return side === 'start' ? (characters[0] ?? '') : (characters.at(-1) ?? '');
};

const endsWithOpeningDelimiter = (text: string): boolean => {
  const characters = Array.from(text);
  const delimiter = characters.at(-1) ?? '';
  if (OPENING_DELIMITER.test(delimiter)) return true;
  if (!SYMMETRIC_DELIMITER.test(delimiter)) return false;
  return !TRAILING_WORD_GRAPHEME.test(characters.slice(0, -1).join(''));
};

const joiningBoundaryCharacter = (text: string, followingText: string): string | undefined => {
  if (TRAILING_TOKEN_JOINER.test(text)) return edgeCharacter(text, 'end');
  if (text.endsWith('.') && LEADING_CALL_DELIMITER.test(followingText)) return '.';
  return undefined;
};

const boundaryCharacter = (text: string, side: 'start' | 'end'): string => {
  const directMention = side === 'start' ? text.match(MENTION_AT_START) : text.match(MENTION_AT_END);
  if (directMention) return edgeCharacter(directMention[1], side);
  if (side === 'end' && endsWithOpeningDelimiter(text)) return edgeCharacter(text, side);
  const boundaryText = side === 'start'
    ? (LEADING_SENTENCE_PUNCTUATION.test(text) ? text : text.replace(LEADING_OUTER_BOUNDARY, ''))
    : text.replace(TRAILING_OUTER_BOUNDARY, '');
  const mention = side === 'start'
    ? boundaryText.match(MENTION_AT_START)
    : boundaryText.match(MENTION_AT_END);
  if (mention) return edgeCharacter(mention[1], side);
  return edgeCharacter(boundaryText, side);
};

const needsBoundarySpace = (
  left: string,
  right: string,
  leftBoundary?: string,
  rightBoundary?: string,
): boolean => {
  const leftChar = leftBoundary ?? boundaryCharacter(left, 'end');
  const rightChar = rightBoundary ?? boundaryCharacter(right, 'start');
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
  if (snapshot.start !== snapshot.end) return normalized;
  const left = currentText.slice(0, snapshot.start);
  const right = currentText.slice(snapshot.end);
  const leftBoundary = snapshot.leftBoundary
    ?? joiningBoundaryCharacter(left, right);
  const transcriptBoundary = joiningBoundaryCharacter(normalized, right);
  return `${needsBoundarySpace(left, normalized, leftBoundary) ? ' ' : ''}${normalized}${
    needsBoundarySpace(normalized, right, transcriptBoundary, snapshot.rightBoundary) ? ' ' : ''
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
): Promise<VoiceCleanupResult> => {
  const startedAt = Date.now();
  const result = (text: string, outcome: VoiceCleanupResult['outcome']): VoiceCleanupResult => ({
    text,
    outcome,
    elapsedMs: Date.now() - startedAt,
  });
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
    if (!response.ok) return result(transcript, 'fallback');
    const payload = await response.json().catch(() => null) as { text?: unknown } | null;
    if (typeof payload?.text !== 'string') return result(transcript, 'fallback');
    if (payload.text !== '' && !payload.text.trim()) return result(transcript, 'fallback');
    return result(payload.text, 'success');
  } catch {
    return result(transcript, 'fallback');
  } finally {
    request.cancel();
  }
};
