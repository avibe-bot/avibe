import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Clock, Copy, Loader2, Mic, Paperclip, Plus, RotateCcw, Send, Square, Trash2, X } from 'lucide-react';
import clsx from 'clsx';

import { useToast } from '../../context/ToastContext';
import { apiFetch } from '../../lib/apiFetch';
import { primeCloudToken } from '../../lib/avibeFetch';
import { isComposingKey } from '../../lib/imeComposition';
import { isSoftKeyboardOpen, isTouchCapableDevice } from '../../lib/softKeyboard';
import { cn, copyTextToClipboard } from '../../lib/utils';
import {
  actionShortcutMatches,
  isPlainEscape,
  useActionShortcutLabel,
  useActionShortcuts,
} from '../../lib/actionShortcuts';
import {
  applyVoiceInsertionWithSnapshot,
  voiceInsertionSnapshot,
  type VoiceInsertionSnapshot,
} from '../../lib/voiceCleanup';
import {
  finalizeVoiceDictation,
  transcribeVoiceSegments,
  VOICE_SEGMENT_MS,
  VOICE_TRANSCRIPTION_CONCURRENCY,
  VoiceTranscriptionQueue,
  type VoiceCleanupOutcome,
  type VoiceTranscriptionSegment,
  VoiceTranscriptionError,
} from '../../lib/voiceTranscription';
import {
  emitVoiceTelemetry,
  type VoiceTelemetryOutcome,
} from '../../lib/voiceTelemetry';
import {
  claimVoiceCapture,
  deleteMapValueIfCurrent,
  isVoiceControlDisabled,
  type VoiceCaptureClaim,
  VoiceRecordingPipeline,
} from '../../lib/voiceRecording';
import {
  VoiceRealtimeSession,
  type VoiceRealtimeFinal,
} from '../../lib/voiceRealtime';
import {
  isWorkbenchUploadRetryable,
  uploadWorkbenchAttachment,
  workbenchUploadErrorTranslationKey,
} from '../../lib/workbenchUpload';
import { Button } from '../ui/button';
import { inForegroundSurface } from './chatShortcuts';
import { useRouteSurfaceWindowEvent } from '../../lib/routeSurfaceActivity';
import {
  MentionEditor,
  type AgentSearchResult,
  type MentionEditorHandle,
  type SessionSearchResult,
} from './MentionEditor';
import type { MentionReference } from '../../lib/mentions';

export type ComposerAttachment = {
  localId: string;
  token: string;
  name: string;
  mime: string;
  size: number;
  kind: 'image' | 'file';
  url: string;
  // Source pixel size for images, returned by the upload endpoint when it could
  // read them — carried through to the persisted attachment so the renderer
  // reserves the image box and loading never shifts the transcript.
  width?: number;
  height?: number;
  status: 'uploading' | 'ready' | 'error';
  retryable?: boolean;
};

// Parallel-upload pool size. Multipart keeps each request binary, but bounded
// concurrency still prevents a large drop from saturating the connection.
const UPLOAD_CONCURRENCY = 4;

// Send and Stop occupy the same pointer target. A fast second click from the
// send gesture can otherwise land on the newly-rendered destructive control.
const STOP_ARM_DELAY_MS = 400;

// Unique-enough id for an optimistic attachment chip before the server token
// lands. Date.now() collides within a batch, so the random suffix separates
// files staged in the same tick.
const newLocalId = () => (
  globalThis.crypto?.randomUUID?.()
  ?? `${Date.now()}-${Math.random().toString(36).slice(2, 14)}`
);

type VoiceSegment = VoiceTranscriptionSegment & {
  task: Promise<void>;
  transcriptionStarted?: boolean;
};

type VoiceRecordingSession = {
  sessionId: string;
  dictationId: string;
  abortController: AbortController;
  segments: VoiceSegment[];
  status: 'recording' | 'transcribing' | 'failed' | 'ready';
  startedAt?: number;
  stoppedAt?: number;
  backlogAtStop?: number;
  finalizedSegmentCount?: number;
  finalizedFailedSegmentCount?: number;
  retryCount: number;
  reportedAttemptCount?: number;
  reportedInsertionAttemptCount?: number;
  transcript?: string;
  error?: unknown;
  captureError?: unknown;
  finalization?: Promise<void>;
  transcriptionQueue: VoiceTranscriptionQueue;
  insertion: VoiceInsertionSnapshot;
  previewInsertion?: VoiceInsertionSnapshot;
  cleanupOutcome?: VoiceCleanupOutcome;
  cleanupElapsedMs?: number;
  realtime?: VoiceRealtimeSession;
  realtimeState?: 'connecting' | 'active' | 'failed' | 'finalized';
  realtimeFirstPreviewAt?: number;
  realtimeFinalAt?: number;
};

// Composer remounts when the user switches chats. Keep pending or retryable
// audio outside the component; successful finalization retains transcript only.
const voiceSessionsById = new Map<string, VoiceRecordingSession>();

const voiceSessionLocksDraft = (session?: VoiceRecordingSession): boolean => (
  session != null && ['recording', 'transcribing', 'ready'].includes(session.status)
);

const newVoiceDictationId = (): string => (
  globalThis.crypto?.randomUUID?.()
  ?? `voice-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`
);

const settleVoiceSession = async (session: VoiceRecordingSession): Promise<void> => {
  const startedAt = Date.now();
  try {
    session.finalizedSegmentCount = session.segments.filter((segment) => segment.blob).length;
    const result = await (async () => {
      try {
        return await finalizeVoiceDictation(session.segments, {
          dictationId: session.dictationId,
          before: session.insertion.before,
          after: session.insertion.after,
        }, {
          signal: session.abortController.signal,
        });
      } finally {
        session.finalizedFailedSegmentCount = session.segments.filter(
          (segment) => segment.error,
        ).length;
      }
    })();
    if (session.abortController.signal.aborted) return;
    session.cleanupOutcome = result.cleanup;
    session.cleanupElapsedMs = Date.now() - startedAt;
    session.transcript = result.text;
    session.segments = [];
    session.error = undefined;
    session.status = 'ready';
  } catch (error) {
    session.cleanupElapsedMs = Date.now() - startedAt;
    session.transcript = undefined;
    session.error = error;
    session.status = 'failed';
  }
};

const runVoiceFinalization = async (session: VoiceRecordingSession): Promise<void> => {
  await Promise.all(session.segments.map((segment) => segment.task));
  if (session.abortController.signal.aborted) {
    deleteMapValueIfCurrent(voiceSessionsById, session.sessionId, session);
    return;
  }
  if (session.captureError !== undefined) {
    session.transcript = undefined;
    session.error = session.captureError;
    session.status = 'failed';
    return;
  }
  await settleVoiceSession(session);
  if (session.abortController.signal.aborted) {
    deleteMapValueIfCurrent(voiceSessionsById, session.sessionId, session);
  }
};

const finalizeVoiceSession = (session: VoiceRecordingSession): Promise<void> => {
  if (session.finalization) return session.finalization;
  session.status = 'transcribing';
  session.finalization = runVoiceFinalization(session);
  return session.finalization;
};

const retryStoredVoiceSession = (session: VoiceRecordingSession): Promise<void> => {
  if (session.captureError !== undefined) return Promise.resolve();
  session.retryCount += 1;
  session.status = 'transcribing';
  session.error = undefined;
  session.finalization = (async () => {
    await transcribeVoiceSegments(session.segments, {
      concurrency: VOICE_TRANSCRIPTION_CONCURRENCY,
      signal: session.abortController.signal,
      dictationId: session.dictationId,
    });
    if (session.abortController.signal.aborted) {
      deleteMapValueIfCurrent(voiceSessionsById, session.sessionId, session);
      return;
    }
    await settleVoiceSession(session);
    if (session.abortController.signal.aborted) {
      deleteMapValueIfCurrent(voiceSessionsById, session.sessionId, session);
    }
  })();
  return session.finalization;
};

const voiceErrorTranslationKey = (error: unknown): string => {
  if (!(error instanceof VoiceTranscriptionError)) return 'chat.compose.voiceFailed';
  if (error.code === 'too_large') return 'chat.compose.voiceTooLarge';
  if (error.code === 'timeout') return 'chat.compose.voiceTimedOut';
  if (error.code === 'unavailable') return 'chat.compose.voiceUnavailable';
  if (error.code === 'empty') return 'chat.compose.voiceEmpty';
  return 'chat.compose.voiceFailed';
};

const voiceTelemetryOutcome = (error: unknown): VoiceTelemetryOutcome =>
  error instanceof VoiceTranscriptionError ? error.code : 'failed';

const canRetryVoiceSession = (session: VoiceRecordingSession): boolean =>
  session.captureError === undefined
  && !(
    session.error instanceof VoiceTranscriptionError
    && session.error.code === 'empty'
  );

const reportVoiceFinalization = (
  session: VoiceRecordingSession,
  outcome: VoiceTelemetryOutcome,
): void => {
  const attemptCount = session.retryCount + 1;
  if (session.reportedAttemptCount === attemptCount) return;
  emitVoiceTelemetry({
    event: 'dictation_finalized',
    outcome,
    dictationId: session.dictationId,
    providerStage: 'finalization',
    attemptCount,
    segmentCount: session.finalizedSegmentCount ?? session.segments.length,
    failedSegmentCount: (
      session.finalizedFailedSegmentCount
      ?? session.segments.filter((segment) => segment.error).length
    ),
    elapsedMs: session.cleanupElapsedMs,
    backlogAtStop: session.backlogAtStop,
    totalDurationMs: session.stoppedAt == null || session.startedAt == null
      ? undefined
      : session.stoppedAt - session.startedAt,
    realtime: session.realtimeState !== undefined,
    firstPreviewMs: session.realtimeFirstPreviewAt == null || session.startedAt == null
      ? undefined
      : session.realtimeFirstPreviewAt - session.startedAt,
    stopToFinalMs: session.realtimeFinalAt == null || session.stoppedAt == null
      ? undefined
      : session.realtimeFinalAt - session.stoppedAt,
    retry: session.retryCount > 0,
  });
  session.reportedAttemptCount = attemptCount;
};

const reportVoiceInsertion = (session: VoiceRecordingSession): void => {
  const attemptCount = session.retryCount + 1;
  if (session.reportedInsertionAttemptCount === attemptCount) return;
  emitVoiceTelemetry({
    event: 'dictation_inserted',
    outcome: 'success',
    dictationId: session.dictationId,
    providerStage: 'finalization',
    attemptCount,
    segmentCount: session.finalizedSegmentCount ?? session.segments.length,
    backlogAtStop: session.backlogAtStop,
    totalDurationMs: session.stoppedAt == null || session.startedAt == null
      ? undefined
      : session.stoppedAt - session.startedAt,
    realtime: session.realtimeState !== undefined,
    firstPreviewMs: session.realtimeFirstPreviewAt == null || session.startedAt == null
      ? undefined
      : session.realtimeFirstPreviewAt - session.startedAt,
    stopToFinalMs: session.realtimeFinalAt == null || session.stoppedAt == null
      ? undefined
      : session.realtimeFinalAt - session.stoppedAt,
    stopToInsertionMs: session.stoppedAt == null
      ? undefined
      : Date.now() - session.stoppedAt,
    retry: session.retryCount > 0,
  });
  session.reportedInsertionAttemptCount = attemptCount;
};

const formatRecordingDuration = (seconds: number): string => {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
};

export interface ComposerProps {
  /** Fired with the trimmed text (+ ready attachments) when the user sends.
   *  Return (or resolve to) ``false`` to signal the send couldn't start, so the
   *  box keeps the text + attachments. */
  onSend: (
    text: string,
    attachments?: ComposerAttachment[],
    references?: MentionReference[],
  ) => boolean | void | Promise<boolean | void>;
  /** A turn is running — the send button becomes a Stop button. Ignored while
   *  ``disabled`` (see the ``busyControls`` note in the body). */
  busy?: boolean;
  /** Pressed while busy. */
  onStop?: () => void;
  /** Seed the box once from a saved draft (chat sessions). */
  initialDraft?: string | null;
  /** Persist draft changes (chat sessions). */
  onDraftChange?: (text: string) => void;
  /** Idle placeholder override; while busy the chat "working" placeholder wins —
   *  except while ``disabled``, where this override always wins so the caller's
   *  reason (e.g. "archived, read-only") is what the user reads. */
  placeholder?: string;
  /** Disable sending (e.g. while the caller creates a session + navigates).
   *  Also suppresses every live-turn control — see ``busyControls``. */
  disabled?: boolean;
  /** Override the row container — e.g. a narrower max-width on the home canvas. */
  className?: string;
  /** When set, enables file upload + voice input scoped to this session. The
   *  Workbench home leaves it unset → a plain text-only composer. */
  sessionId?: string;
  /** Focus the textarea on mount (desktop only — skipped on touch devices so it
   *  never pops the on-screen keyboard). The chat composer remounts per session,
   *  so this also covers opening / switching sessions. */
  autoFocus?: boolean;
  /** When BOTH are provided, the input upgrades to the rich mention editor:
   *  `@` autocompletes enabled Agents, `#` autocompletes Sessions. Leaving them
   *  unset keeps the plain textarea (e.g. the Workbench home). */
  onSearchAgents?: (query: string) => Promise<AgentSearchResult[]>;
  onSearchSessions?: (query: string) => Promise<SessionSearchResult[]>;
}

export interface ComposerHandle {
  /** Stage + upload files from outside the composer — e.g. a chat-page-wide
   *  drag-and-drop that drops onto the transcript rather than the input row. */
  addFiles: (files: File[]) => void;
  /** Insert a `#<session>` reference chip at the cursor (e.g. "reference this
   *  session" from the sidebar). No-op when the mention editor isn't active
   *  (the plain-textarea home composer). */
  insertSessionReference: (sessionId: string, title?: string | null) => void;
  /** Append text to the end of the composer (e.g. a quoted chat selection),
   *  with a separating space when the composer is non-empty. No-op when the
   *  mention editor isn't active (the plain-textarea home composer). */
  appendText: (text: string) => void;
  /** Handle the Chat page's configured voice chord. Starting may be gated by
   *  the page, while an active recording can always be completed. */
  handleVoiceShortcut: (event: KeyboardEvent, allowStart: boolean) => boolean;
}

// The chat-style input row: an auto-growing textarea + a Send/Stop icon button,
// plus (when ``sessionId`` is set) attachment upload and voice input on the left.
// Shared by the chat view (ChatPage) and the Workbench home so both use one input
// component instead of each hand-rolling its own. Owns its draft value; callers
// react via onSend / onDraftChange. design.pen kxEkn.
export const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer({
  onSend,
  busy = false,
  onStop,
  initialDraft = null,
  onDraftChange,
  placeholder,
  disabled = false,
  className,
  sessionId,
  autoFocus = false,
  onSearchAgents,
  onSearchSessions,
}, ref) {
  const { t } = useTranslation();
  const { voiceInput: voiceInputShortcut } = useActionShortcuts();
  const voiceShortcutLabel = useActionShortcutLabel(voiceInputShortcut);
  const voiceShortcutHint = t('chat.compose.voiceShortcutHint', { shortcut: voiceShortcutLabel });
  const { showToast } = useToast();
  const [value, setValue] = useState('');
  // The mention editor (Lexical) OWNS its text, so we don't mirror every
  // keystroke into ``value`` there — a fast third-party IME (e.g. a voice
  // keyboard like Typeless) inserts text in bursts, and re-rendering on each
  // burst can interrupt its in-progress insertion and truncate the input. For
  // that path we keep the live text in ``valueRef`` and track only whether the
  // box is non-empty (``hasText``) for the Send button; ``value`` still backs the
  // plain-textarea (home) path. See onChange below.
  const [hasText, setHasText] = useState(false);
  const composerRootRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const valueRef = useRef('');
  // Seed once from a saved draft, but only while the box is untouched so a
  // late-arriving draft can't clobber live typing.
  const draftAppliedRef = useRef(false);
  // Blocks a same-tick double-submit before the optimistic clear re-renders.
  const pendingRef = useRef(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const attachmentFilesRef = useRef(new Map<string, File>());
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [asrAvailable, setAsrAvailable] = useState(false);
  const [asrMaxFileBytes, setAsrMaxFileBytes] = useState<number | null>(null);
  const [recordingStarting, setRecordingStarting] = useState(false);
  const [recording, setRecording] = useState(false);
  const [restoringRecording, setRestoringRecording] = useState(() => (
    sessionId != null && voiceSessionLocksDraft(voiceSessionsById.get(sessionId))
  ));
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const [realtimeAnnouncement, setRealtimeAnnouncement] = useState('');
  const [transcribing, setTranscribing] = useState(false);
  const [voiceRetainedSession, setVoiceRetainedSession] = useState<VoiceRecordingSession | null>(null);
  const transcribingRef = useRef(false);
  const recorderRef = useRef<VoiceRecordingPipeline | null>(null);
  const voiceCaptureClaimRef = useRef<VoiceCaptureClaim | null>(null);
  const recordingSessionRef = useRef<VoiceRecordingSession | null>(null);
  const recordingStartRef = useRef(false);
  const pendingVoiceInsertionRef = useRef<VoiceInsertionSnapshot | null>(null);
  const focusNextVoiceControlRef = useRef(false);
  const voiceEditorFocusReturnRef = useRef<HTMLElement | null>(null);
  const finishVoiceControlRef = useRef<HTMLButtonElement | null>(null);
  const recordingTickerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const unmountedRef = useRef(false);
  const disabledRef = useRef(disabled);

  // Upload + voice are scoped to a session (the upload endpoint needs one); the
  // home composer leaves them off.
  const mediaEnabled = Boolean(sessionId);
  const voiceDraftReadOnly = recordingStarting || recording || restoringRecording || transcribing;

  useLayoutEffect(() => {
    disabledRef.current = disabled;
  }, [disabled]);

  const clearRecordingTimers = useCallback(() => {
    if (recordingTickerRef.current != null) clearInterval(recordingTickerRef.current);
    recordingTickerRef.current = null;
  }, []);

  // Mentions upgrade the plain textarea to a Lexical editor (chat composer only,
  // gated on the caller wiring both search sources). The editor owns the rich
  // content; ``valueRef`` holds its live serialized marker text for the
  // send/draft path (``hasText`` gates the Send button) and ``referencesRef``
  // holds the resolved sidecar for the send payload.
  const useMentions = Boolean(onSearchAgents && onSearchSessions);
  const mentionRef = useRef<MentionEditorHandle | null>(null);
  const referencesRef = useRef<MentionReference[]>([]);

  useEffect(() => {
    // The mention editor seeds itself from ``initialText`` and drives ``value``
    // via onChange, so the textarea-path seeding is skipped there.
    if (useMentions || draftAppliedRef.current || initialDraft == null) return;
    draftAppliedRef.current = true;
    if (initialDraft) setValue((cur) => (cur ? cur : initialDraft));
  }, [initialDraft, useMentions]);

  // Keep a ref of the latest value so the async voice-fill can append without a
  // stale closure. The mention path keeps ``valueRef`` current itself (in
  // onChange), since it doesn't drive ``value`` — so don't clobber it here.
  useEffect(() => {
    if (useMentions) return;
    valueRef.current = value;
  }, [value, useMentions]);

  // Auto-grow the textarea with its content. ``min-h-9`` floors it at the 36px
  // send-button height so a single line sits vertically centered against the
  // button; ``max-h-40`` (160px) caps it, after which it scrolls.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [value]);

  // Desktop only: focus the textarea on mount so opening a chat (and — via the
  // per-session remount — switching sessions) lands the cursor in the input.
  // Skipped on touch devices so it never pops the on-screen keyboard.
  useEffect(() => {
    if (!autoFocus || isTouchCapableDevice()) return;
    textareaRef.current?.focus();
  }, [autoFocus]);

  // The mic button only appears when transcription is wired up (Vibe Cloud
  // paired + enabled), so it never dead-ends on click.
  useEffect(() => {
    if (!mediaEnabled) return;
    let alive = true;
    apiFetch('/api/asr/status')
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!alive) return;
        const available = Boolean(data?.available);
        setAsrAvailable(available);
        setAsrMaxFileBytes(
          typeof data?.max_file_bytes === 'number'
            && Number.isFinite(data.max_file_bytes)
            && data.max_file_bytes > 0
            ? Math.floor(data.max_file_bytes)
            : null,
        );
        // Prewarm the cloud token so the first recording uploads straight to
        // avibe.bot with no mint latency (no-op when the cloud is unavailable).
        if (available) primeCloudToken();
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [mediaEnabled]);

  // Finish capture + suppress post-unmount setState if the composer unmounts.
  // Chat navigation is not a discard action: the session-keyed batch continues
  // finalizing and is restored when the user returns.
  useEffect(() => {
    // Reset on setup (StrictMode runs cleanup→setup twice on mount, so a stale
    // ``true`` from the first cleanup would otherwise wedge voice input).
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
      if (recordingSessionRef.current) {
        recordingSessionRef.current.previewInsertion = undefined;
      }
      try {
        if (recordingStartRef.current) {
          recordingSessionRef.current?.abortController.abort();
          recorderRef.current?.abort();
        } else {
          recorderRef.current?.finish();
        }
      } catch {
        /* already stopped */
      }
      voiceCaptureClaimRef.current?.release();
      voiceCaptureClaimRef.current = null;
      clearRecordingTimers();
    };
  }, [clearRecordingTimers]);

  // Clear staged attachments when the session changes so a chip uploaded in one
  // chat can't ride into another — defense in depth on top of ChatPage keying
  // the composer by session.
  useEffect(() => {
    setAttachments([]);
    attachmentFilesRef.current.clear();
    setRealtimeAnnouncement('');
    setVoiceRetainedSession(null);
    setRestoringRecording(
      sessionId != null && voiceSessionLocksDraft(voiceSessionsById.get(sessionId)),
    );
  }, [sessionId]);

  const removeAttachment = (localId: string) => {
    attachmentFilesRef.current.delete(localId);
    setAttachments((cur) => cur.filter((a) => a.localId !== localId));
  };

  // Upload one already-staged file, then flip its chip to ready or retryable.
  // ``sid`` is threaded in so the fan-out below stays typed after the guard.
  const uploadOne = async (file: File, localId: string, sid: string) => {
    try {
      const json = await uploadWorkbenchAttachment(sid, file, localId);
      if (attachmentFilesRef.current.get(localId) !== file) return;
      attachmentFilesRef.current.delete(localId);
      setAttachments((cur) =>
        cur.map((a) =>
          a.localId === localId
            ? {
                ...a,
                token: json.token,
                url: json.url,
                mime: json.mime || a.mime,
                size: json.size ?? a.size,
                kind: json.kind || a.kind,
                width: typeof json.width === 'number' ? json.width : undefined,
                height: typeof json.height === 'number' ? json.height : undefined,
                status: 'ready',
                retryable: undefined,
              }
            : a,
        ),
      );
    } catch (error) {
      if (attachmentFilesRef.current.get(localId) !== file) return;
      const retryable = isWorkbenchUploadRetryable(error);
      if (!retryable) attachmentFilesRef.current.delete(localId);
      setAttachments((cur) => cur.map((a) => (
        a.localId === localId ? { ...a, status: 'error', retryable } : a
      )));
      showToast(t(workbenchUploadErrorTranslationKey(error)), 'error');
    }
  };

  const retryAttachment = (localId: string) => {
    if (!sessionId || disabledRef.current || voiceDraftReadOnly) return;
    const file = attachmentFilesRef.current.get(localId);
    if (!file) return;
    setAttachments((cur) => cur.map((a) => (
      a.localId === localId ? { ...a, status: 'uploading', retryable: undefined } : a
    )));
    void uploadOne(file, localId, sessionId);
  };

  // Upload a whole batch — multi-select from the picker or a chat-page drag-drop
  // (handed in via the imperative handle below). Stage every chip up front in one
  // update so the user sees the full batch at once, then upload with bounded
  // concurrency so a big drop of large files doesn't flood memory / the endpoint.
  const uploadFiles = async (files: File[]) => {
    if (!sessionId) return;
    const sid = sessionId;
    const staged = files.map((file) => ({ file, localId: newLocalId() }));
    for (const { file, localId } of staged) attachmentFilesRef.current.set(localId, file);
    setAttachments((cur) => [
      ...cur,
      ...staged.map(({ file, localId }) => ({
        localId,
        token: '',
        name: file.name,
        mime: file.type || 'application/octet-stream',
        size: file.size,
        kind: file.type.startsWith('image/') ? ('image' as const) : ('file' as const),
        url: '',
        status: 'uploading' as const,
      })),
    ]);
    const queue = [...staged];
    const worker = async () => {
      let item = queue.shift();
      while (item) {
        await uploadOne(item.file, item.localId, sid);
        item = queue.shift();
      }
    };
    await Promise.all(Array.from({ length: Math.min(UPLOAD_CONCURRENCY, queue.length) }, worker));
  };

  // Expose file staging so a chat-page-wide drop zone (ChatPage) can hand off
  // files dropped onto the transcript — the picker + chips still live here. No
  // deps array → always runs the latest uploadFiles closure for this session.
  const voiceDraftLocked = () => (
    recordingStartRef.current
    || recordingSessionRef.current !== null
    || transcribingRef.current
    || (
      sessionId != null
      && voiceSessionLocksDraft(voiceSessionsById.get(sessionId))
    )
  );
  const captureVoiceInsertion = useCallback((): VoiceInsertionSnapshot => {
    if (useMentions) {
      return mentionRef.current?.captureSelection()
        ?? voiceInsertionSnapshot(valueRef.current, valueRef.current.length, valueRef.current.length);
    }
    const editor = textareaRef.current;
    return voiceInsertionSnapshot(
      valueRef.current,
      editor?.selectionStart ?? valueRef.current.length,
      editor?.selectionEnd ?? valueRef.current.length,
    );
  }, [useMentions]);

  const insertVoiceTranscript = useCallback((session: VoiceRecordingSession): boolean => {
    // Replace the active preview (or the original captured range when realtime
    // never produced one) with the finalized transcript as one draft edit.
    if (useMentions) {
      const inserted = mentionRef.current?.commitVoicePreview(
        session.insertion,
        session.transcript ?? '',
      ) ?? false;
      if (inserted) setRealtimeAnnouncement('');
      return inserted;
    }
    const current = valueRef.current;
    const result = applyVoiceInsertionWithSnapshot(
      current,
      session.previewInsertion ?? session.insertion,
      session.transcript ?? '',
      session.insertion,
    );
    if (result === null) return false;
    session.previewInsertion = undefined;
    valueRef.current = result.text;
    setValue(result.text);
    setRealtimeAnnouncement('');
    onDraftChange?.(result.text);
    const caret = session.insertion.start + result.insertion.length;
    requestAnimationFrame(() => textareaRef.current?.setSelectionRange(caret, caret));
    return true;
  }, [onDraftChange, useMentions]);

  const restoreRealtimePreview = useCallback((session: VoiceRecordingSession): void => {
    setRealtimeAnnouncement('');
    if (useMentions) {
      mentionRef.current?.restoreVoicePreview();
      return;
    }
    const preview = session.previewInsertion;
    session.previewInsertion = undefined;
    if (preview === undefined || valueRef.current !== preview.text) return;
    valueRef.current = session.insertion.text;
    setValue(session.insertion.text);
  }, [useMentions]);

  const showRealtimePreview = useCallback((
    session: VoiceRecordingSession,
    preview: string,
  ): void => {
    const normalized = preview.trim();
    if (!normalized) {
      restoreRealtimePreview(session);
      return;
    }
    if (useMentions) {
      if (mentionRef.current?.showVoicePreview(session.insertion, normalized)) {
        setRealtimeAnnouncement(normalized);
      }
      return;
    }
    const current = valueRef.current;
    const result = applyVoiceInsertionWithSnapshot(
      current,
      session.previewInsertion ?? session.insertion,
      normalized,
      session.insertion,
    );
    if (result === null) return;
    session.previewInsertion = result.snapshot;
    valueRef.current = result.text;
    setValue(result.text);
    setRealtimeAnnouncement(normalized);
  }, [restoreRealtimePreview, useMentions]);

  const retainSettledVoiceSession = useCallback((session: VoiceRecordingSession): void => {
    restoreRealtimePreview(session);
    setVoiceRetainedSession(session);
  }, [restoreRealtimePreview]);

  const queueVoiceSegment = (
    session: VoiceRecordingSession,
    blob: Blob,
    durationMs: number,
    overlapMs?: number,
    final = false,
  ) => {
    const segment: VoiceSegment = {
      blob,
      sequence: session.segments.length,
      final,
      durationMs,
      overlapMs,
      task: Promise.resolve(),
    };
    session.segments.push(segment);
    if (session.realtimeState === 'connecting' || session.realtimeState === 'active') {
      return;
    }
    segment.transcriptionStarted = true;
    segment.task = session.transcriptionQueue.enqueue(segment);
  };

  const activateHttpFallback = (session: VoiceRecordingSession): void => {
    if (session.realtimeState === 'failed') {
      for (const segment of session.segments) {
        if (!segment.transcriptionStarted && segment.blob && !segment.final) {
          segment.transcriptionStarted = true;
          segment.task = session.transcriptionQueue.enqueue(segment);
        }
      }
    }
  };

  const presentVoiceSession = useCallback((session: VoiceRecordingSession) => {
    const activeHere = (
      !unmountedRef.current
      && sessionId === session.sessionId
      && voiceSessionsById.get(session.sessionId) === session
    );
    if (session.status === 'ready' && session.transcript) {
      reportVoiceFinalization(session, session.cleanupOutcome ?? 'success');
    } else if (session.status === 'failed') {
      reportVoiceFinalization(session, voiceTelemetryOutcome(session.error));
    }
    if (!activeHere) return;

    if (session.status === 'ready' && session.transcript) {
      if (disabledRef.current) {
        retainSettledVoiceSession(session);
        return;
      }
      if (!insertVoiceTranscript(session)) {
        retainSettledVoiceSession(session);
        showToast(t('chat.compose.voiceDraftChanged'), 'error');
        return;
      }
      voiceSessionsById.delete(session.sessionId);
      setVoiceRetainedSession(null);
      reportVoiceInsertion(session);
      return;
    }
    if (session.status === 'failed') {
      retainSettledVoiceSession(session);
      showToast(t(voiceErrorTranslationKey(session.error)), 'error');
    }
  }, [insertVoiceTranscript, retainSettledVoiceSession, sessionId, showToast, t]);

  const finishVoiceSession = async (session: VoiceRecordingSession) => {
    const activeHere = !unmountedRef.current && sessionId === session.sessionId;
    if (activeHere) {
      transcribingRef.current = true;
      setTranscribing(true);
      setVoiceRetainedSession(session);
    }
    try {
      await finalizeVoiceSession(session);
      presentVoiceSession(session);
    } finally {
      if (recordingSessionRef.current === session) recordingSessionRef.current = null;
      if (activeHere) {
        transcribingRef.current = false;
        if (!unmountedRef.current) setTranscribing(false);
      }
    }
  };

  const finishRealtimeVoiceSession = async (session: VoiceRecordingSession) => {
    const activeHere = !unmountedRef.current && sessionId === session.sessionId;
    if (activeHere) {
      transcribingRef.current = true;
      setTranscribing(true);
      setVoiceRetainedSession(session);
    }
    session.status = 'transcribing';
    session.finalization = (async () => {
      try {
        if (session.captureError !== undefined || !session.realtime) {
          throw session.captureError ?? new Error('realtime_unavailable');
        }
        const result: VoiceRealtimeFinal = await session.realtime.finish();
        if (session.abortController.signal.aborted) return;
        if (!result.text.trim()) throw new VoiceTranscriptionError('empty');
        session.realtimeState = 'finalized';
        session.realtimeFinalAt = Date.now();
        session.cleanupOutcome = result.cleanup;
        session.cleanupElapsedMs = session.realtimeFinalAt - (session.stoppedAt ?? session.realtimeFinalAt);
        session.finalizedSegmentCount = session.segments.filter((segment) => segment.blob).length;
        session.finalizedFailedSegmentCount = 0;
        session.transcript = result.text;
        session.segments = [];
        session.error = undefined;
        session.status = 'ready';
      } catch (_error) {
        if (session.abortController.signal.aborted) return;
        session.realtimeState = 'failed';
        activateHttpFallback(session);
        session.status = 'transcribing';
        await runVoiceFinalization(session);
      }
    })();
    try {
      await session.finalization;
      presentVoiceSession(session);
    } finally {
      if (recordingSessionRef.current === session) recordingSessionRef.current = null;
      if (activeHere) {
        transcribingRef.current = false;
        if (!unmountedRef.current) setTranscribing(false);
      }
    }
  };

  const retryVoiceSession = async (session: VoiceRecordingSession) => {
    if (unmountedRef.current || transcribingRef.current) return;
    transcribingRef.current = true;
    setTranscribing(true);
    try {
      await retryStoredVoiceSession(session);
      presentVoiceSession(session);
    } finally {
      transcribingRef.current = false;
      if (!unmountedRef.current) setTranscribing(false);
    }
  };

  const discardVoiceSession = useCallback((session: VoiceRecordingSession) => {
    session.abortController.abort();
    restoreRealtimePreview(session);
    deleteMapValueIfCurrent(voiceSessionsById, session.sessionId, session);
    setVoiceRetainedSession((current) => (current === session ? null : current));
  }, [restoreRealtimePreview]);

  const copyReadyVoiceTranscript = useCallback(async (session: VoiceRecordingSession) => {
    if (!session.transcript) return;
    if (await copyTextToClipboard(session.transcript)) {
      showToast(t('chat.compose.voiceTranscriptCopied'), 'success');
    }
  }, [showToast, t]);

  useEffect(() => {
    if (!sessionId) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const restore = () => {
      if (!alive) return;
      const session = voiceSessionsById.get(sessionId);
      if (!session) {
        setRestoringRecording(false);
        return;
      }
      if (session.status === 'recording') {
        setRestoringRecording(true);
        timer = setTimeout(restore, 50);
        return;
      }
      setRestoringRecording(false);
      if (session.status === 'transcribing' && session.finalization) {
        transcribingRef.current = true;
        setTranscribing(true);
        setVoiceRetainedSession(session);
        void session.finalization.finally(() => {
          if (!alive) return;
          transcribingRef.current = false;
          setTranscribing(false);
          presentVoiceSession(session);
        });
        return;
      }
      presentVoiceSession(session);
    };
    restore();
    return () => {
      alive = false;
      if (timer != null) clearTimeout(timer);
    };
  }, [presentVoiceSession, sessionId]);

  const startRecording = async (capturedInsertion?: VoiceInsertionSnapshot) => {
    if (
      !sessionId
      || recordingStartRef.current
      || voiceSessionsById.has(sessionId)
    ) {
      return;
    }
    const insertion = capturedInsertion ?? captureVoiceInsertion();
    recordingStartRef.current = true;
    setRecordingStarting(true);
    let stream: MediaStream | null = null;
    let startingPipeline: VoiceRecordingPipeline | null = null;
    let startingSession: VoiceRecordingSession | null = null;
    const captureClaim = claimVoiceCapture(() => {
      try {
        if (startingPipeline) startingPipeline.finish();
        else stream?.getTracks().forEach((track) => track.stop());
      } catch {
        startingPipeline?.abort();
        stream?.getTracks().forEach((track) => track.stop());
      }
    });
    voiceCaptureClaimRef.current = captureClaim;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!captureClaim.isCurrent() || unmountedRef.current || disabledRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }

      const abortController = new AbortController();
      const dictationId = newVoiceDictationId();
      const session: VoiceRecordingSession = {
        sessionId,
        dictationId,
        abortController,
        segments: [],
        status: 'recording',
        retryCount: 0,
        insertion,
        transcriptionQueue: new VoiceTranscriptionQueue({
          concurrency: VOICE_TRANSCRIPTION_CONCURRENCY,
          signal: abortController.signal,
          dictationId,
        }),
      };
      session.realtimeState = 'connecting';
      session.realtime = new VoiceRealtimeSession({
        before: insertion.before,
        after: insertion.after,
        signal: abortController.signal,
        onPreview: (preview) => {
          if (recordingSessionRef.current !== session || unmountedRef.current) return;
          session.realtimeFirstPreviewAt ??= Date.now();
          showRealtimePreview(session, `${preview.text}${preview.stash}`.trim());
        },
        onError: () => {
          if (abortController.signal.aborted) return;
          session.realtimeState = 'failed';
          activateHttpFallback(session);
        },
      });
      void session.realtime.start().then(() => {
        if (abortController.signal.aborted) return;
        session.realtimeState = 'active';
      }).catch(() => {
        if (abortController.signal.aborted) return;
        session.realtimeState = 'failed';
        activateHttpFallback(session);
      });
      startingSession = session;
      const pipeline = new VoiceRecordingPipeline({
        stream,
        segmentMs: VOICE_SEGMENT_MS,
        maxFileBytes: asrMaxFileBytes,
        onSegment: (blob, metadata) => queueVoiceSegment(
          session,
          blob,
          metadata.durationMs,
          metadata.overlapMs,
          metadata.final,
        ),
        onPcm: (samples) => {
          if (session.realtimeState === 'connecting' || session.realtimeState === 'active') {
            session.realtime?.sendPcm(samples);
          }
        },
        onStopRequested: (reason, metadata) => {
          if (reason === 'abort') return;
          session.stoppedAt = metadata.requestedAt;
          session.backlogAtStop = session.segments.filter(
            (segment) => !segment.final && !segment.receipt && !segment.error,
          ).length;
        },
        onError: (error) => {
          session.captureError = error;
          if (session.realtime) {
            session.realtimeState = 'failed';
            session.realtime.abort();
            activateHttpFallback(session);
          }
          if (!voiceSessionsById.has(session.sessionId)) {
            voiceSessionsById.set(session.sessionId, session);
          }
        },
        onStopped: (reason, metadata) => {
          captureClaim.release();
          if (voiceCaptureClaimRef.current === captureClaim) voiceCaptureClaimRef.current = null;
          if (recorderRef.current === pipeline) recorderRef.current = null;
          if (recordingSessionRef.current === session) recordingSessionRef.current = null;
          clearRecordingTimers();
          if (!unmountedRef.current) setRecording(false);

          if (reason === 'abort') {
            session.abortController.abort();
            session.realtime?.abort();
            restoreRealtimePreview(session);
            deleteMapValueIfCurrent(voiceSessionsById, session.sessionId, session);
            return;
          }
          session.backlogAtStop = (session.backlogAtStop ?? 0) + metadata.pendingSegmentCount;
          if (!session.segments.length && session.captureError === undefined) {
            session.realtime?.abort();
            restoreRealtimePreview(session);
            reportVoiceFinalization(session, 'empty');
            if (!unmountedRef.current && sessionId === session.sessionId) {
              showToast(t('chat.compose.voiceEmpty'), 'error');
            }
            deleteMapValueIfCurrent(voiceSessionsById, session.sessionId, session);
            return;
          }
          if (session.segments.length && !session.segments.at(-1)?.final) {
            session.segments.push({
              blob: null,
              sequence: session.segments.length,
              final: true,
              durationMs: 0,
              task: Promise.resolve(),
            });
          }
          if (session.realtime && session.realtimeState !== 'failed') {
            void finishRealtimeVoiceSession(session);
          } else {
            void finishVoiceSession(session);
          }
        },
      });
      startingPipeline = pipeline;
      recordingSessionRef.current = session;
      recorderRef.current = pipeline;
      const captureActive = await pipeline.start();
      if (!captureActive) return;
      if (!captureClaim.isCurrent()) {
        pipeline.finish();
        return;
      }
      if (unmountedRef.current || disabledRef.current) {
        pipeline.abort();
        return;
      }
      const startedAt = Date.now();
      session.startedAt = startedAt;

      const previous = voiceSessionsById.get(sessionId);
      if (previous) {
        // A retained batch may have settled while microphone setup was pending.
        // Preserve it until the user explicitly retries or discards it.
        pipeline.abort();
        return;
      }
      voiceSessionsById.set(sessionId, session);
      setVoiceRetainedSession(null);
      setRecordingSeconds(0);
      recordingTickerRef.current = setInterval(() => {
        setRecordingSeconds(Math.floor((Date.now() - startedAt) / 1000));
      }, 1000);
      setRecording(true);
    } catch {
      // getUserMedia may have handed us a live stream before capture setup
      // threw. Release it so the mic does not stay on.
      startingSession?.abortController.abort();
      startingSession?.realtime?.abort();
      if (startingSession) restoreRealtimePreview(startingSession);
      if (recorderRef.current === startingPipeline) recorderRef.current = null;
      if (recordingSessionRef.current === startingSession) recordingSessionRef.current = null;
      captureClaim.release();
      if (voiceCaptureClaimRef.current === captureClaim) voiceCaptureClaimRef.current = null;
      clearRecordingTimers();
      stream?.getTracks().forEach((track) => track.stop());
      if (!unmountedRef.current) {
        setRecording(false);
        showToast(t(
          stream
            ? 'chat.compose.voiceStartFailed'
            : 'chat.compose.voicePermissionFailed',
        ), 'error');
      }
    } finally {
      if (recorderRef.current !== startingPipeline || recordingSessionRef.current !== startingSession) {
        captureClaim.release();
        if (voiceCaptureClaimRef.current === captureClaim) voiceCaptureClaimRef.current = null;
      }
      recordingStartRef.current = false;
      if (!unmountedRef.current) setRecordingStarting(false);
    }
  };

  const stopRecording = useCallback(() => {
    recorderRef.current?.finish();
  }, []);
  const abortRecording = useCallback(() => {
    recordingSessionRef.current?.abortController.abort();
    recorderRef.current?.abort();
  }, []);
  const toggleRecording = (pointerActivation: boolean) => {
    const pointerInsertion = pointerActivation ? pendingVoiceInsertionRef.current : null;
    pendingVoiceInsertionRef.current = null;
    if (recording) stopRecording();
    else void startRecording(pointerInsertion ?? captureVoiceInsertion());
  };

  // ESC aborts an in-progress recording (discard, no transcribe).
  useRouteSurfaceWindowEvent('keydown', (event) => {
    if (isPlainEscape(event)) {
      event.preventDefault();
      abortRecording();
    }
  }, recording);

  const trimmed = value.trim();
  const readyAttachments = attachments.filter((a) => a.status === 'ready');
  const uploading = attachments.some((a) => a.status === 'uploading');
  // The mention path doesn't mirror its text into ``value`` (see onChange), so
  // its "is there text?" signal comes from ``hasText``; the textarea path reads
  // ``value`` directly.
  const hasComposerText = useMentions ? hasText : trimmed.length > 0;
  // Send on text OR a ready attachment (attachment-only runs a turn so the agent
  // reads the files); blocked while an upload is still in flight, and while
  // recording so neither the Send button nor the Enter key can fire mid-record.
  const canSubmit =
    (hasComposerText || readyAttachments.length > 0)
    && !uploading
    && !disabled
    && !voiceDraftReadOnly;
  // ``busy && disabled`` is an incoherent pair for ANY caller: ``disabled`` means
  // this composer may not act on the session, yet the busy branch renders an
  // ENABLED Stop button (it never consulted ``disabled``) and its "working"
  // placeholder overrides the caller's own reason for disabling. Resolve it here,
  // at the shared component, so no call site has to remember to pre-clear ``busy``.
  //
  // It is reachable, not theoretical: archiving a session with a running turn
  // commits the status flip and only THEN cancels the controller turn over the
  // internal socket (``archive_session``'s docstring — it can't join the
  // transaction). ChatPage bootstraps ``busy`` from the controller's
  // ``turn_state.foreground``, not the row's ``agent_status``, so an archived chat
  // opened inside that window loads read-only AND busy.
  const busyControls = busy && !disabled;
  const voiceProcessing = transcribing || recordingStarting || restoringRecording;
  const voiceControlMode = voiceProcessing
    ? 'loading'
    : recording
      ? 'finish'
      : voiceRetainedSession?.status === 'ready'
        ? 'copy'
        : voiceRetainedSession && canRetryVoiceSession(voiceRetainedSession)
          ? 'retry'
          : asrAvailable
            ? 'record'
            : null;
  const voiceFlowActive = (
    recording
    || voiceProcessing
    || voiceRetainedSession !== null
  );
  const voiceCaptureActive = recording || voiceProcessing;
  const voiceDiscardAvailable = recording || voiceRetainedSession !== null;
  const voiceShortcutAvailable = (
    (voiceControlMode === 'record' || voiceControlMode === 'finish')
    && !isVoiceControlDisabled(
      disabled,
      recording,
      voiceProcessing,
      Boolean(voiceRetainedSession),
    )
  );
  const [stopArmed, setStopArmed] = useState(false);

  useLayoutEffect(() => {
    if (!focusNextVoiceControlRef.current) return;
    if (voiceControlMode === 'finish') {
      finishVoiceControlRef.current?.focus({ preventScroll: true });
      focusNextVoiceControlRef.current = false;
    } else if (voiceControlMode !== 'loading') {
      focusNextVoiceControlRef.current = false;
    }
  }, [voiceControlMode]);

  useLayoutEffect(() => {
    if (voiceDraftReadOnly || voiceEditorFocusReturnRef.current === null) return;
    const target = voiceEditorFocusReturnRef.current;
    voiceEditorFocusReturnRef.current = null;
    const activeElement = document.activeElement;
    if (
      target.isConnected
      && (activeElement === null || activeElement === document.body || !activeElement.isConnected)
    ) {
      target.focus({ preventScroll: true });
    }
  }, [voiceDraftReadOnly]);

  const handleVoiceShortcut = useCallback((event: KeyboardEvent, allowStart: boolean): boolean => {
    if (
      !voiceShortcutAvailable
      || event.defaultPrevented
      || event.repeat
      || Boolean(composerRootRef.current?.querySelector('[data-mention-picker]'))
      || !actionShortcutMatches(event, voiceInputShortcut)
    ) {
      return false;
    }
    if (
      voiceControlMode === 'record'
      && (!allowStart || inForegroundSurface(event.target as Element | null))
    ) return false;

    event.preventDefault();
    if (voiceControlMode === 'finish') stopRecording();
    else {
      const activeElement = document.activeElement;
      const editorFocus = (
        activeElement === textareaRef.current
        || (
          activeElement instanceof HTMLElement
          && activeElement.getAttribute('contenteditable') === 'true'
          && composerRootRef.current?.contains(activeElement)
        )
      ) ? activeElement as HTMLElement : null;
      voiceEditorFocusReturnRef.current = editorFocus;
      // Keep the page's current focus when the shortcut started outside the
      // editor. Editor starts still move to Finish, then restore the caret.
      focusNextVoiceControlRef.current = editorFocus !== null;
      void startRecording(captureVoiceInsertion());
    }
    return true;
  }, [
    captureVoiceInsertion,
    startRecording,
    stopRecording,
    voiceControlMode,
    voiceInputShortcut,
    voiceShortcutAvailable,
  ]);

  // ChatPage owns the window listener because the shortcut applies across that
  // page, while the composer remains the sole owner of voice state and actions.
  // No deps array keeps every exposed operation on the latest render state.
  useImperativeHandle(ref, () => ({
    addFiles: (files: File[]) => void uploadFiles(files),
    insertSessionReference: (refSessionId: string, title?: string | null) => {
      if (voiceDraftLocked()) return;
      // Same node a typed `#` pick yields: trigger `#`, label = title||id, data
      // carries the stable sessionId → serializes to `#<id>` + a session ref.
      mentionRef.current?.insertMention('#', title?.trim() || refSessionId, { sessionId: refSessionId });
    },
    appendText: (text: string) => {
      if (!voiceDraftLocked()) mentionRef.current?.append(text);
    },
    handleVoiceShortcut,
  }));

  useEffect(() => {
    if (!busyControls) {
      setStopArmed(false);
      return;
    }
    setStopArmed(false);
    const timer = window.setTimeout(() => setStopArmed(true), STOP_ARM_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [busyControls, sessionId]);

  const update = (next: string) => {
    valueRef.current = next;
    setValue(next);
    onDraftChange?.(next);
  };

  const submit = async () => {
    if (!canSubmit || pendingRef.current) return;
    // Mention path: the live text lives in valueRef (value isn't mirrored there).
    const submitted = (useMentions ? valueRef.current : value).trim();
    const sent = readyAttachments;
    const sentRefs = referencesRef.current;
    pendingRef.current = true;
    // Clear optimistically so the box can't be re-submitted and a slow send can't
    // wipe text typed in the meantime.
    valueRef.current = '';
    setValue('');
    setHasText(false);
    onDraftChange?.('');
    setAttachments([]);
    attachmentFilesRef.current.clear();
    referencesRef.current = [];
    if (useMentions) mentionRef.current?.clear();
    try {
      // If the caller reports the send couldn't start (home no-project nudge),
      // restore the prompt + attachments for retry — unless the user typed anew.
      const started = await onSend(submitted, sent, useMentions ? sentRefs : undefined);
      if (started === false) {
        if (!unmountedRef.current) {
          setAttachments((cur) => (cur.length ? cur : sent));
        }
        if (!valueRef.current.trim()) {
          // Only restore when the user hasn't started a new draft during the
          // in-flight send. Persist the restored text as a draft too: the
          // optimistic blank was already cached before onSend rejected it. The
          // callback still runs after navigation unmounts this Composer, because
          // the original session's local draft must survive even though its UI
          // state no longer exists.
          valueRef.current = submitted;
          if (!unmountedRef.current) {
            setValue(submitted);
            setHasText(Boolean(submitted));
            if (useMentions) {
              // Chips re-resolve when re-picked; the content is never lost.
              referencesRef.current = sentRefs;
              mentionRef.current?.setText(submitted);
            }
          }
          onDraftChange?.(submitted);
        }
      }
    } finally {
      pendingRef.current = false;
    }
  };

  return (
    <div
      ref={composerRootRef}
      className={cn('mx-auto flex w-full max-w-[1080px] flex-col gap-2', className)}
    >
      {mediaEnabled && attachments.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {attachments.map((att) => (
            <div
              key={att.localId}
              className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 py-1 pl-1.5 pr-1 text-[12px]"
            >
              {att.status === 'uploading' ? (
                <span className="grid size-7 place-items-center rounded text-muted">
                  <Loader2 className="size-4 animate-spin" />
                </span>
              ) : att.kind === 'image' && att.url ? (
                <img src={att.url} alt="" className="size-7 rounded object-cover" />
              ) : (
                <span className="grid size-7 place-items-center rounded bg-cyan/15 text-cyan-ink">
                  <Paperclip className="size-3.5" />
                </span>
              )}
              <span className={clsx('max-w-[160px] truncate', att.status === 'error' ? 'text-pink-ink' : 'text-foreground')}>
                {att.name}
              </span>
              {att.status === 'error' && att.retryable && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={disabled || voiceDraftReadOnly}
                  onClick={() => retryAttachment(att.localId)}
                  aria-label={t('chat.compose.retryAttachment')}
                  className="size-5 shrink-0 text-muted hover:text-foreground"
                >
                  <RotateCcw className="size-3.5" />
                </Button>
              )}
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => removeAttachment(att.localId)}
                aria-label={t('chat.compose.removeAttachment')}
                className="size-5 shrink-0 text-muted hover:text-foreground"
              >
                <X className="size-3.5" />
              </Button>
            </div>
          ))}
        </div>
      )}
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {realtimeAnnouncement
          ? `${t('chat.compose.voicePreview')}: ${realtimeAnnouncement}`
          : ''}
      </div>
      <div
        className={cn(
          'flex w-full items-end gap-1.5 rounded-2xl border border-border-strong bg-surface-2 py-2 pr-2 shadow-[0_-4px_24px_-12px_rgba(0,0,0,0.5)]',
          mediaEnabled ? 'pl-1.5' : 'pl-3.5',
        )}
      >
        {/* Attach stays in the first idle slot and voice starts in the second.
            Once a voice flow starts, the unrelated attachment control withdraws
            and the active voice action takes the left edge. */}
        {mediaEnabled && (
          <div className="flex items-end">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              disabled={disabled || voiceDraftReadOnly}
              className="hidden"
              onChange={(e) => {
                if (e.target.files?.length) void uploadFiles(Array.from(e.target.files));
                e.target.value = '';
              }}
            />
            {!voiceFlowActive && (
              /* Attach uses a generic plus rather than a paperclip so it reads
                 as the general "add anything" entry point. */
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => fileInputRef.current?.click()}
                disabled={disabled}
                aria-label={t('chat.compose.attach')}
                className="h-9 w-7 shrink-0"
              >
                <Plus className="size-4" />
              </Button>
            )}
            {voiceControlMode === 'record' && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onPointerDown={() => {
                  pendingVoiceInsertionRef.current = captureVoiceInsertion();
                }}
                onPointerCancel={() => {
                  pendingVoiceInsertionRef.current = null;
                }}
                onContextMenu={() => {
                  pendingVoiceInsertionRef.current = null;
                }}
                onClick={(event) => {
                  focusNextVoiceControlRef.current = true;
                  toggleRecording(event.detail > 0);
                }}
                disabled={isVoiceControlDisabled(
                  disabled,
                  recording,
                  voiceProcessing,
                  Boolean(voiceRetainedSession),
                )}
                aria-label={t('chat.compose.voice')}
                title={voiceShortcutAvailable ? voiceShortcutHint : t('chat.compose.voice')}
                className="size-9 shrink-0"
              >
                <Mic className="size-4" />
              </Button>
            )}
            {voiceControlMode === 'finish' && (
              <Button
                ref={finishVoiceControlRef}
                type="button"
                variant="default"
                size="icon"
                onClick={stopRecording}
                aria-label={t('chat.compose.stopRecording')}
                title={voiceShortcutAvailable ? voiceShortcutHint : t('chat.compose.stopRecording')}
                className="h-9 w-12 shrink-0"
              >
                <Check className="size-[18px]" strokeWidth={2.5} />
              </Button>
            )}
            {voiceControlMode === 'loading' && (
              <Button
                type="button"
                variant="secondary"
                size="icon"
                disabled
                aria-label={t('chat.compose.voiceProcessing')}
                className="size-9 shrink-0"
              >
                <Loader2 className="size-4 animate-spin" />
              </Button>
            )}
            {voiceControlMode === 'retry' && voiceRetainedSession && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => void retryVoiceSession(voiceRetainedSession)}
                aria-label={t('chat.compose.voiceRetry')}
                title={t('chat.compose.voiceRetry')}
                className="size-9 shrink-0"
              >
                <RotateCcw className="size-4" />
              </Button>
            )}
            {voiceControlMode === 'copy' && voiceRetainedSession && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => void copyReadyVoiceTranscript(voiceRetainedSession)}
                aria-label={t('chat.compose.voiceCopyTranscript')}
                title={t('chat.compose.voiceCopyTranscript')}
                className="size-9 shrink-0"
              >
                <Copy className="size-4" />
              </Button>
            )}
            {recording && (
              <span
                className="flex h-9 w-10 items-center justify-center text-xs tabular-nums text-muted"
                aria-live="off"
              >
                {formatRecordingDuration(recordingSeconds)}
              </span>
            )}
          </div>
        )}
        {useMentions ? (
          <MentionEditor
            ref={mentionRef}
            className="flex-1"
            placeholder={busyControls ? t('chat.compose.placeholderBusy') : placeholder ?? t('chat.compose.placeholder')}
            disabled={disabled || voiceDraftReadOnly}
            autoFocus={autoFocus}
            initialText={initialDraft}
            onSearchAgents={onSearchAgents!}
            onSearchSessions={onSearchSessions!}
            // Paste-to-upload: only when this composer has an upload target
            // (a session), mirroring the picker + drag-drop. uploadFiles itself
            // no-ops without a session, so this gate is also defense in depth.
            // ``!disabled`` for the same reason the picker and mic are gated: a
            // pasted file could never be sent, and the upload endpoint has no
            // archive guard, so it would persist an orphan attachment. The
            // editor is also set non-editable while disabled, which should keep
            // the paste event from reaching Lexical at all — this is the
            // explicit gate rather than a reliance on that.
            onPasteFiles={mediaEnabled && !disabled && !voiceDraftReadOnly
              ? (files) => void uploadFiles(files)
              : undefined}
            onChange={(text, references, isDraftSeed, isVoicePreview) => {
              // The editor owns the text; keep the live copy in a ref (no
              // re-render) so a fast IME isn't interrupted mid-insert, and only
              // re-render when the empty/non-empty state actually flips (React
              // bails on an unchanged boolean). Send/draft read valueRef/text.
              valueRef.current = text;
              referencesRef.current = references;
              setHasText(text.trim().length > 0);
              // A mount-time draft RESTORE is not a user edit: re-persisting it
              // is at best a redundant write, and under a stale-props remount it
              // would save the previous session's draft under this session id
              // (mirrors the plain-textarea path, whose seeding never saves).
              if (!isDraftSeed && !isVoicePreview) onDraftChange?.(text);
            }}
            onSubmit={submit}
          />
        ) : (
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => update(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends, Shift+Enter newline — EXCEPT while the on-screen
              // keyboard is open (mobile), where Enter inserts a newline and Send is
              // the button. Hardware keyboards (no soft keyboard) keep Enter-to-send.
              // ``isComposingKey`` guards against submitting mid-IME composition (CJK).
              if (e.key === 'Enter' && !e.shiftKey && !isComposingKey(e) && !isSoftKeyboardOpen()) {
                e.preventDefault();
                submit();
              }
            }}
            rows={1}
            // The MentionEditor path honours ``disabled``; this plain path never
            // did, so a disabled composer still accepted typing (only the Send
            // button was inert). Fixed here so every caller inherits it.
            disabled={disabled}
            readOnly={voiceDraftReadOnly}
            placeholder={busyControls ? t('chat.compose.placeholderBusy') : placeholder ?? t('chat.compose.placeholder')}
            className="max-h-40 min-h-9 flex-1 resize-none bg-transparent py-2 text-[13px] leading-5 text-foreground outline-none placeholder:text-muted"
          />
        )}
        {/* A running agent turn keeps its Stop escape hatch even during voice
            capture. Queue/Send withdraw while capture owns the draft. */}
        {busyControls ? (
          <>
            {/* Sending while a turn runs is allowed — the backend enqueues it
                (202) instead of refusing (Enter does this too). Surface a visible
                affordance for it, but only when there's something to send: a cyan
                "queue" button left of Stop. Paper-plane + a small clock badge
                reads as "send, but later / into the queue". */}
            {!voiceCaptureActive && canSubmit && (
              <Button
                type="button"
                variant="accent"
                size="icon"
                onClick={submit}
                aria-label={t('chat.compose.queueSend')}
                title={t('chat.compose.queueSend')}
                className="size-9 shrink-0"
              >
                <span className="relative inline-flex">
                  <Send className="size-4" />
                  <Clock
                    className="absolute -bottom-1 -right-1 size-2.5 rounded-full bg-surface-2"
                    strokeWidth={2.5}
                  />
                </span>
              </Button>
            )}
            <Button
              type="button"
              variant="destructive-soft"
              size="icon"
              onClick={onStop}
              disabled={!stopArmed}
              aria-label={t('chat.compose.stop')}
              className="size-9 shrink-0"
            >
              <Square className="size-4" />
            </Button>
          </>
        ) : !voiceCaptureActive ? (
          <Button
            type="button"
            variant="default"
            size="icon"
            onClick={submit}
            disabled={!canSubmit}
            aria-label={t('chat.compose.send')}
            className="size-9 shrink-0"
          >
            <Send className="size-4" />
          </Button>
        ) : null}
        {/* Destructive voice actions stay at the opposite edge from the primary
            voice slot. Finish is never adjacent to Trash; once a failed or
            recovered recording is retained, Send remains available before the
            rightmost Trash action. */}
        {voiceDiscardAvailable && (
          <Button
            type="button"
            variant="destructive-soft"
            size="icon"
            onClick={() => {
              if (recording) abortRecording();
              else if (voiceRetainedSession) discardVoiceSession(voiceRetainedSession);
            }}
            aria-label={t(
              recording
                ? 'chat.compose.cancelRecording'
                : 'chat.compose.voiceDiscard',
            )}
            className="size-9 shrink-0"
          >
            <Trash2 className="size-4" />
          </Button>
        )}
      </div>
    </div>
  );
});
