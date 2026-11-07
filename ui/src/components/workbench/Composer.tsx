import type { TranslationKey } from '@/i18n/types';
import {
  forwardRef,
  Suspense,
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
import { nativeHapticImpact } from '../../lib/native';
import { dismissSoftKeyboard, isSoftKeyboardOpen, isTouchCapableDevice } from '../../lib/softKeyboard';
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
  VOICE_SEGMENT_MS,
  VOICE_TRANSCRIPTION_CONCURRENCY,
  VoiceTranscriptionQueue,
  VOICE_SETTLE_RETRY_WINDOW_MS,
  type VoiceCleanupOutcome,
  type VoiceTranscriptionSegment,
  VoiceTranscriptionError,
} from '../../lib/voiceTranscription';
import { createVoiceActivity, reportVoiceSilenceGate, type VoiceActivity } from '../../lib/voiceActivity';
import { VoiceTakeKeeper } from '../../lib/voiceTakeKeeper';
import {
  emitVoiceTelemetry,
  type VoiceTelemetryOutcome,
} from '../../lib/voiceTelemetry';
import {
  claimVoiceCapture,
  isVoiceCaptureActive,
  deleteMapValueIfCurrent,
  isVoiceControlDisabled,
  prewarmVoiceCapture,
  type VoiceCaptureClaim,
  VoiceRecordingPipeline,
} from '../../lib/voiceRecording';
import {
  VoiceRealtimeSession,
  type VoiceRealtimeFinal,
} from '../../lib/voiceRealtime';
import { resolveVoiceContext, type VoiceContext, type VoiceContextSource } from '../../lib/voiceContext';
import { VOICE_MESSAGE_METADATA, type VoiceMessageMetadata } from '../../lib/voiceMessage';
import {
  ComposerHoldCard,
  ComposerHoldSurface,
  type HoldPointer,
} from '../voice/ComposerHold';
import { MOUSE_HOLD_GESTURE, useHoldToTalk } from '../voice/useHoldToTalk';
import type { VoiceTarget } from '../voice/VoiceCard';
import {
  MAX_WORKBENCH_ATTACHMENT_BYTES,
  isWorkbenchUploadRetryable,
  uploadWorkbenchAttachment,
  workbenchUploadErrorTranslationKey,
} from '../../lib/workbenchUpload';
import { Button } from '../ui/button';
import { useImageViewer } from '../ui/image-viewer-context';
import { inForegroundSurface } from './chatShortcuts';
import { useRouteSurfaceWindowEvent } from '../../lib/routeSurfaceActivity';
import { useLatestRef } from '../../lib/useLatestRef';
import { revealComposerAfterNativeKeyboardResize } from '../../lib/nativeKeyboard';
import type {
  MentionSearchResult,
  MentionEditorHandle,
  SessionSearchResult,
} from './MentionEditor';
import { DeferredMentionEditor } from './deferredMentionEditor';
import type { MentionReference } from '../../lib/mentions';
import { beginVoiceStatusRead, knownVoiceStatus } from '../../lib/voiceAvailability';

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
  status: 'staged' | 'uploading' | 'ready' | 'error';
  /** Original file for a home draft; uploaded only after explicit Send. */
  file?: File;
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

// The leading slot of a staged-attachment chip: the spinner while the file
// uploads, the paperclip for anything that is not a rendered image, and the
// thumbnail once an image upload has its media URL.
//
// That last case is also the preview trigger (#2100): a screenshot has to be
// inspectable at full size BEFORE it is sent, and the thumbnail is the thing on
// screen that means "this image". It is a real button so the keyboard reaches
// it, named after the file so two staged screenshots are told apart, and it
// opens the shared lightbox over the staged images alone — the composer's own
// set, never the transcript's.
//
// Only a ``ready`` chip previews. ``uploading`` and ``error`` have no media URL
// to show, and the home composer's ``staged`` chips carry a local blob URL the
// viewer's download action cannot serve — they keep the plain thumbnail they
// render today. Where no lightbox is mounted at all the trigger is not rendered
// either, rather than a focusable control that would do nothing.
const AttachmentThumbnail = ({ att, gallery }: { att: ComposerAttachment; gallery: string[] }) => {
  const { t } = useTranslation();
  const imageViewer = useImageViewer();
  if (att.status === 'uploading') {
    return (
      <span className="grid size-7 place-items-center rounded text-muted">
        <Loader2 className="size-4 animate-spin" />
      </span>
    );
  }
  if (att.kind !== 'image' || !att.url) {
    return (
      <span className="grid size-7 place-items-center rounded bg-cyan/15 text-cyan-ink">
        <Paperclip className="size-3.5" />
      </span>
    );
  }
  const thumbnail = <img src={att.url} alt="" className="size-7 rounded object-cover" />;
  if (att.status !== 'ready' || !imageViewer) return thumbnail;
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      onClick={() => imageViewer.open(att.url, { gallery })}
      aria-label={t('chat.compose.previewAttachment', { name: att.name })}
      className="size-7 shrink-0 cursor-zoom-in overflow-hidden rounded p-0 hover:bg-transparent"
    >
      {thumbnail}
    </Button>
  );
};

type VoiceSegment = VoiceTranscriptionSegment & {
  task: Promise<void>;
  transcriptionStarted?: boolean;
};

type VoiceRecordingSession = {
  sessionId: string;
  dictationId: string;
  abortController: AbortController;
  /** Aborted when capture stops: segments sent while recording stop retrying on their own, and
   *  finalization takes them over within its bounded window. */
  captureRetries: AbortController;
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
  /** A live preview carried words: the take held speech, whatever its levels say. */
  realtimeHeardWords?: boolean;
  realtimeFinalAt?: number;
  /** The conversation this dictation is spoken into, read once when it starts: realtime
   *  recognition, its HTTP fallback, and a retry all send the same. */
  context: Promise<VoiceContext>;
  /** How much of what the microphone heard was loud enough to be speech. */
  activity: VoiceActivity;
  /** The take kept on the device until its text is inserted (lib/voiceTakeStore). */
  keeper?: VoiceTakeKeeper;
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
          context: session.context,
          retryUntil: Date.now() + VOICE_SETTLE_RETRY_WINDOW_MS,
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
    session.keeper?.transcribed(result.text);
  } catch (error) {
    session.cleanupElapsedMs = Date.now() - startedAt;
    session.transcript = undefined;
    session.error = error;
    session.status = 'failed';
    session.keeper?.pending();
  }
};

const runVoiceFinalization = async (session: VoiceRecordingSession): Promise<void> => {
  await Promise.all(session.segments.map((segment) => segment.task));
  if (session.abortController.signal.aborted) {
    deleteMapValueIfCurrent(voiceSessionsById, session.sessionId, session);
    return;
  }
  // A capture that broke midway keeps what it recorded until then: only one that recorded
  // nothing (or only silence) fails here.
  if (session.captureError !== undefined && !voiceSessionHasAudio(session)) {
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

// Sends the kept audio again, through whichever path works now: finalization sends the segments
// that have no receipt yet, then the last one.
const retryStoredVoiceSession = (session: VoiceRecordingSession): Promise<void> => {
  if (!canRetryVoiceSession(session)) return Promise.resolve();
  session.retryCount += 1;
  session.status = 'transcribing';
  session.error = undefined;
  session.finalization = (async () => {
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

const voiceErrorTranslationKey = (session: VoiceRecordingSession): TranslationKey => {
  const { error } = session;
  if (voiceSessionVoicedEmpty(session)) return 'chat.compose.voiceEmptyKept';
  if (!(error instanceof VoiceTranscriptionError)) return 'chat.compose.voiceFailed';
  if (error.code === 'too_large') return 'chat.compose.voiceTooLarge';
  if (error.code === 'timeout') return 'chat.compose.voiceTimedOut';
  if (error.code === 'unavailable') return 'chat.compose.voiceUnavailable';
  if (error.code === 'empty') return 'chat.compose.voiceEmpty';
  return 'chat.compose.voiceFailed';
};

const voiceTelemetryOutcome = (error: unknown): VoiceTelemetryOutcome =>
  error instanceof VoiceTranscriptionError ? error.code : 'failed';

/** Whether the session recorded audio worth transcribing: some segment, and not a capture
 *  whose graph never rendered a frame (silence). */
function voiceSessionHasAudio(session: VoiceRecordingSession): boolean {
  if (session.captureError instanceof VoiceTranscriptionError && session.captureError.code === 'empty') {
    return false;
  }
  return session.segments.some((segment) => segment.blob !== null);
}

/** Whether the empty transcript of this session is a recognition that failed: the microphone
 *  heard sound loud enough for speech. */
const voiceSessionVoicedEmpty = (session: VoiceRecordingSession): boolean => (
  session.error instanceof VoiceTranscriptionError
  && session.error.code === 'empty'
  && session.activity.voiced()
);

// A take keeps its audio and can be sent again unless there is nothing to send: no audio, or an
// empty transcript of a take that was silent.
function canRetryVoiceSession(session: VoiceRecordingSession): boolean {
  if (!voiceSessionHasAudio(session)) return false;
  return !(
    session.error instanceof VoiceTranscriptionError
    && session.error.code === 'empty'
    && !session.activity.voiced()
  );
}

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
    /** Set on a message spoken by holding the composer (``{ input: 'voice' }``). */
    metadata?: Record<string, unknown>,
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
  /** The "working" placeholder override (a Bot's conversation names the Bot). */
  busyPlaceholder?: string;
  /** The idle placeholder where a hold would record, when the caller overrides
   *  ``placeholder`` (it names the gesture: "…, or hold to talk"). */
  holdPlaceholder?: string;
  /** Who a message spoken by holding the composer goes to, for its card: a Bot
   *  (its name and avatar) or a Session (``title``). */
  voiceTarget?: VoiceTarget;
  /** A message sent while busy reaches the running Turn at once instead of
   *  waiting for it (a Bot's conversation, docs/plans/bots.md D4): the button
   *  then says Send, without the "later" badge. */
  busySendsNow?: boolean;
  /** Disable sending (e.g. while the caller creates a session + navigates).
   *  Also suppresses every live-turn control — see ``busyControls``. */
  disabled?: boolean;
  /** Keep a retained draft editable while an uncertain send is inspected. */
  sendDisabled?: boolean;
  /** Override the row container — e.g. a narrower max-width on the home canvas. */
  className?: string;
  /** ``inline`` (default): one row — Attach and Voice, the input, Send.
   *  ``stacked``: the input takes the whole first row; the action row below keeps
   *  Attach on the left and ``actions``, Voice and Send on the right, so the text
   *  gets the full width and no control moves as it grows. */
  layout?: 'inline' | 'stacked';
  /** Caller-owned controls for the stacked action row, placed just before Voice
   *  (the chat's Agent/model picker). Ignored by the inline layout. */
  actions?: React.ReactNode;
  /** Upload + retained voice scope for an existing chat session. */
  sessionId?: string;
  /** Home drafts stage local files and transcribe voice without a server session. */
  stageMedia?: boolean;
  /** Focus the textarea on mount (desktop only — skipped on touch devices so it
   *  never pops the on-screen keyboard). The chat composer remounts per session,
   *  so this also covers opening / switching sessions. */
  autoFocus?: boolean;
  /** When BOTH are provided, the input upgrades to the rich mention editor:
   *  `@` autocompletes enabled Agents, `#` autocompletes Sessions. Leaving them
   *  unset keeps the plain textarea (e.g. the Workbench home). */
  onSearchAgents?: (query: string) => Promise<MentionSearchResult[]>;
  onSearchSessions?: (query: string) => Promise<SessionSearchResult[]>;
  /** The conversation this composer writes into (its Agent's latest reply, the user's previous
   *  message), read when a dictation starts (a tap, ⌥Z, or a hold) and sent to recognition.
   *  Surfaces without a conversation leave it unset. */
  readVoiceContext?: () => VoiceContextSource;
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
  /** Replace the whole draft from outside and focus the input with the caret at
   *  the end — the home's suggestion cards seed a starting task the user then
   *  edits. It never sends, and refuses while voice capture owns the draft. */
  setDraft: (text: string) => void;
  /** Handle the Chat page's configured voice chord. Starting may be gated by
   *  the page, while an active recording can always be completed. */
  handleVoiceShortcut: (event: KeyboardEvent, allowStart: boolean) => boolean;
}

/** A press on one of the box's controls is that control's, never a hold. */
const isInteractiveTarget = (target: EventTarget | null): boolean => (
  target instanceof Element
  && Boolean(target.closest('button, a, input, select, label, [role="button"], [role="menuitem"], [role="option"], [data-mention-picker]'))
);

const isEditableTarget = (target: EventTarget | null): boolean => (
  target instanceof HTMLTextAreaElement || (target instanceof HTMLElement && target.isContentEditable)
);

// The chat-style input row: an auto-growing textarea + a Send/Stop icon button,
// plus (when ``sessionId`` is set) attachment upload and voice input on the left.
// Shared by the chat view (ChatPage) and the Workbench home so both use one input
// component instead of each hand-rolling its own. Owns its draft value; callers
// react via onSend / onDraftChange. ui/design: chat/composer/draft.
export const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer({
  onSend,
  busy = false,
  onStop,
  initialDraft = null,
  onDraftChange,
  placeholder,
  busyPlaceholder,
  holdPlaceholder,
  voiceTarget,
  busySendsNow = false,
  disabled = false,
  sendDisabled = false,
  className,
  layout = 'inline',
  actions,
  sessionId,
  stageMedia = false,
  autoFocus = false,
  onSearchAgents,
  onSearchSessions,
  readVoiceContext,
}, ref) {
  const { t } = useTranslation();
  const { voiceInput: voiceInputShortcut } = useActionShortcuts();
  const voiceShortcutLabel = useActionShortcutLabel(voiceInputShortcut);
  const voiceShortcutHint = t('chat.compose.voiceShortcutHint', { shortcut: voiceShortcutLabel });
  const { showToast } = useToast();
  const [value, setValue] = useState('');
  const [homeVoiceKey] = useState(() => `draft:${newLocalId()}`);
  const voiceKey = sessionId ?? (stageMedia ? homeVoiceKey : undefined);
  // Where a take spoken here is kept until its words land (lib/voiceTakeStore): this Session, or
  // the Home draft, which a reload starts afresh under a new key but the same place.
  const voiceKeepTarget = sessionId ?? (stageMedia ? 'home' : null);
  const stagedUrlsRef = useRef(new Set<string>());
  // The mention editor (Lexical) OWNS its text, so we don't mirror every
  // keystroke into ``value`` there — a fast third-party IME (e.g. a voice
  // keyboard like Typeless) inserts text in bursts, and re-rendering on each
  // burst can interrupt its in-progress insertion and truncate the input. For
  // that path we keep the live text in ``valueRef`` and track only whether the
  // box is non-empty (``hasText``) for the Send button; ``value`` still backs the
  // plain-textarea (home) path. See onChange below.
  //
  // The mention editor seeds itself with the saved draft when it mounts, and the
  // change event for that seed may never come: Lexical's OnChangePlugin drops
  // the editor's first committed update, which a browser commits together with
  // the editor's initial state. So that text starts here, as the editor's does,
  // and a restored draft can be sent (or queued) without being edited first.
  const useMentions = Boolean(onSearchAgents && onSearchSessions);
  const [hasText, setHasText] = useState(() => useMentions && Boolean(initialDraft?.trim()));
  const composerRootRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const valueRef = useRef(useMentions ? initialDraft ?? '' : '');
  // The saved draft the box was last seeded with, and whether the user has
  // edited it since it mounted. A later ``initialDraft`` (the saved draft read
  // again, e.g. when a chat painted from its snapshot gets its bootstrap)
  // replaces an untouched draft as a seed; it never overwrites the user's input.
  const seededDraftRef = useRef<string | null>(initialDraft);
  const draftEditedRef = useRef(false);
  // Blocks a same-tick double-submit before the optimistic clear re-renders.
  const pendingRef = useRef(false);
  // Set by setDraft; consumed by the layout effect that focuses after the seeded
  // text has rendered.
  const seedFocusRef = useRef(false);
  const cancelSeedRevealRef = useRef<() => void>(() => undefined);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const attachmentFilesRef = useRef(new Map<string, File>());
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  // Voice starts from the last known ``/api/asr/status`` answer for this reader
  // (lib/voiceAvailability), so the control is in place from the first frame;
  // the live read below then decides. Once shown, the control keeps its place
  // for this mount: a live "unavailable" disables it instead of removing it, so
  // nothing around it moves.
  const [asrAvailable, setAsrAvailable] = useState(() => (
    (Boolean(sessionId) || stageMedia) && knownVoiceStatus()?.available === true
  ));
  const [voiceSlotHeld, setVoiceSlotHeld] = useState(asrAvailable);
  const [asrMaxFileBytes, setAsrMaxFileBytes] = useState<number | null>(() => knownVoiceStatus()?.maxFileBytes ?? null);
  const [recordingStarting, setRecordingStarting] = useState(false);
  const [recording, setRecording] = useState(false);
  const [restoringRecording, setRestoringRecording] = useState(() => (
    voiceKey != null && voiceSessionLocksDraft(voiceSessionsById.get(voiceKey))
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
  const voiceEditorFocusReturnRef = useRef<{
    target: HTMLElement;
    previousFocus: Element | null;
  } | null>(null);
  const voiceTextareaCaretRef = useRef<{ start: number; end: number } | null>(null);
  const finishVoiceControlRef = useRef<HTMLButtonElement | null>(null);
  const recordingTickerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const unmountedRef = useRef(false);
  const disabledRef = useRef(disabled);

  const mediaEnabled = Boolean(sessionId) || stageMedia;
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
  const mentionRef = useRef<MentionEditorHandle | null>(null);
  const referencesRef = useRef<MentionReference[]>([]);

  useEffect(() => {
    // The mention editor seeds itself from ``initialText`` on mount; the
    // textarea path, and every later saved draft, seed here.
    if (initialDraft == null) return;
    const mountSeed = useMentions && seededDraftRef.current === initialDraft;
    seededDraftRef.current = initialDraft;
    if (mountSeed || draftEditedRef.current || initialDraft === valueRef.current) return;
    // Voice capture owns the draft while it runs.
    if (
      recordingStartRef.current
      || recordingSessionRef.current !== null
      || transcribingRef.current
      || (voiceKey != null && voiceSessionLocksDraft(voiceSessionsById.get(voiceKey)))
    ) return;
    valueRef.current = initialDraft;
    setHasText(initialDraft.trim().length > 0);
    if (useMentions) mentionRef.current?.seedText(initialDraft);
    else setValue(initialDraft);
  }, [initialDraft, useMentions, voiceKey]);

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
    // Prepare the AudioWorklet while the Chat page is idle. No microphone is
    // opened here; this only removes worklet compilation from the first word.
    void prewarmVoiceCapture().catch(() => undefined);
    let alive = true;
    const statusRead = beginVoiceStatusRead();
    apiFetch('/api/asr/status')
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        // A failed read says nothing about availability: keep what is known.
        if (!alive || !data) return;
        const available = Boolean(data.available);
        const maxFileBytes = typeof data?.max_file_bytes === 'number'
          && Number.isFinite(data.max_file_bytes)
          && data.max_file_bytes > 0
          ? Math.floor(data.max_file_bytes)
          : null;
        setAsrAvailable(available);
        if (available) setVoiceSlotHeld(true);
        setAsrMaxFileBytes(maxFileBytes);
        statusRead.remember({ available, maxFileBytes });
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
      voiceKey != null && voiceSessionLocksDraft(voiceSessionsById.get(voiceKey)),
    );
  }, [sessionId, voiceKey]);

  useEffect(() => () => {
    for (const url of stagedUrlsRef.current) URL.revokeObjectURL(url);
    // A home draft has no durable chat to restore into after a real departure.
    const voice = voiceSessionsById.get(homeVoiceKey);
    // Its audio stays on the device, for the Home composer's pending voice items.
    voice?.keeper?.release();
    voice?.abortController.abort();
    voiceSessionsById.delete(homeVoiceKey);
  }, [homeVoiceKey]);

  const removeAttachment = (localId: string) => {
    const url = attachments.find((attachment) => attachment.localId === localId)?.url;
    if (url && stagedUrlsRef.current.delete(url)) URL.revokeObjectURL(url);
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
    if (!mediaEnabled || disabledRef.current || pendingRef.current || voiceDraftLocked()) return;
    if (stageMedia && !sessionId) {
      const staged: ComposerAttachment[] = [];
      for (const file of files) {
        if (!file.size || file.size > MAX_WORKBENCH_ATTACHMENT_BYTES) {
          showToast(t(file.size ? 'chat.compose.attachmentTooLarge' : 'chat.compose.attachmentEmpty'), 'error');
          continue;
        }
        const url = file.type.startsWith('image/') ? URL.createObjectURL(file) : '';
        if (url) stagedUrlsRef.current.add(url);
        staged.push({ localId: newLocalId(), file, token: '', name: file.name,
          mime: file.type || 'application/octet-stream', size: file.size,
          kind: file.type.startsWith('image/') ? 'image' : 'file', url, status: 'staged' });
      }
      setAttachments((cur) => [...cur, ...staged]);
      return;
    }
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
      voiceKey != null
      && voiceSessionLocksDraft(voiceSessionsById.get(voiceKey))
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
      ) ?? null;
      if (inserted !== null) setRealtimeAnnouncement('');
      return inserted !== null;
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
    draftEditedRef.current = true;
    onDraftChange?.(result.text);
    const caret = session.insertion.start + result.insertion.length;
    voiceTextareaCaretRef.current = { start: caret, end: caret };
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
    voiceTextareaCaretRef.current = {
      start: session.insertion.start,
      end: session.insertion.end,
    };
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
    session.keeper?.segment(segment, blob);
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
      && voiceKey === session.sessionId
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
      session.keeper?.forget();
      reportVoiceInsertion(session);
      return;
    }
    if (session.status === 'failed') {
      retainSettledVoiceSession(session);
      showToast(t(voiceErrorTranslationKey(session)), 'error');
    }
  }, [insertVoiceTranscript, retainSettledVoiceSession, voiceKey, showToast, t]);

  const finishVoiceSession = async (session: VoiceRecordingSession) => {
    const activeHere = !unmountedRef.current && voiceKey === session.sessionId;
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
    const activeHere = !unmountedRef.current && voiceKey === session.sessionId;
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
        session.keeper?.transcribed(result.text);
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
    session.keeper?.forget();
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
    if (!voiceKey) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const restore = () => {
      if (!alive) return;
      const session = voiceSessionsById.get(voiceKey);
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
  }, [presentVoiceSession, voiceKey]);

  const startRecording = async (capturedInsertion?: VoiceInsertionSnapshot) => {
    if (
      !voiceKey
      || recordingStartRef.current
      || voiceSessionsById.has(voiceKey)
    ) {
      return;
    }
    const insertion = capturedInsertion ?? captureVoiceInsertion();
    if (!useMentions) {
      voiceTextareaCaretRef.current = {
        start: insertion.start,
        end: insertion.end,
      };
    }
    const context = resolveVoiceContext(readVoiceContext?.());
    // Start compiling the capture graph in parallel with the permission and
    // lock steps. The microphone is still requested only by getUserMedia below.
    void prewarmVoiceCapture().catch(() => undefined);
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
      await captureClaim.ready;
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!captureClaim.isCurrent() || unmountedRef.current || disabledRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }

      const abortController = new AbortController();
      const captureRetries = new AbortController();
      const dictationId = newVoiceDictationId();
      const session: VoiceRecordingSession = {
        sessionId: voiceKey,
        dictationId,
        abortController,
        captureRetries,
        segments: [],
        status: 'recording',
        retryCount: 0,
        insertion,
        context,
        activity: createVoiceActivity(),
        keeper: voiceKeepTarget ? new VoiceTakeKeeper(dictationId, { target: voiceKeepTarget, intent: 'insert' }) : undefined,
        transcriptionQueue: new VoiceTranscriptionQueue({
          concurrency: VOICE_TRANSCRIPTION_CONCURRENCY,
          signal: abortController.signal,
          retrySignal: captureRetries.signal,
          dictationId,
          context,
        }),
      };
      session.realtimeState = 'connecting';
      session.realtime = new VoiceRealtimeSession({
        before: insertion.before,
        after: insertion.after,
        context,
        signal: abortController.signal,
        onPreview: (preview) => {
          if (recordingSessionRef.current !== session || unmountedRef.current) return;
          session.realtimeFirstPreviewAt ??= Date.now();
          if (`${preview.text}${preview.stash}`.trim()) session.realtimeHeardWords = true;
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
      session.keeper?.begin();
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
          // What would be transcribed, its flushed tail included, is what the gate judges.
          session.activity.observePcm(samples);
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
          session.captureRetries.abort();
          captureClaim.release();
          if (voiceCaptureClaimRef.current === captureClaim) voiceCaptureClaimRef.current = null;
          if (recorderRef.current === pipeline) recorderRef.current = null;
          if (recordingSessionRef.current === session) recordingSessionRef.current = null;
          clearRecordingTimers();
          if (!unmountedRef.current) setRecording(false);

          if (reason === 'abort') {
            session.keeper?.forget();
            session.abortController.abort();
            session.realtime?.abort();
            restoreRealtimePreview(session);
            deleteMapValueIfCurrent(voiceSessionsById, session.sessionId, session);
            return;
          }
          session.backlogAtStop = (session.backlogAtStop ?? 0) + metadata.pendingSegmentCount;
          // Nothing recorded, or a take that neither showed live words nor got as loud as a
          // soft syllable (the silence gate, lib/voiceActivity): no speech, and recognition is
          // not asked, since on near-silence it can return its hotwords as text.
          const silent = session.activity.silent() && !session.realtimeHeardWords;
          if (silent) reportVoiceSilenceGate(session.activity, session.stoppedAt === undefined || session.startedAt === undefined ? undefined : session.stoppedAt - session.startedAt);
          if ((!session.segments.length && session.captureError === undefined) || silent) {
            session.keeper?.forget();
            session.abortController.abort();
            session.realtime?.abort();
            restoreRealtimePreview(session);
            reportVoiceFinalization(session, 'empty');
            if (!unmountedRef.current && voiceKey === session.sessionId) {
              showToast(t('chat.compose.voiceEmpty'), 'error');
            }
            deleteMapValueIfCurrent(voiceSessionsById, session.sessionId, session);
            return;
          }
          if (session.segments.length && !session.segments.at(-1)?.final) {
            const marker: VoiceSegment = {
              blob: null,
              sequence: session.segments.length,
              final: true,
              durationMs: 0,
              task: Promise.resolve(),
            };
            session.segments.push(marker);
            session.keeper?.segment(marker, null);
          }
          session.keeper?.pending();
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

      const previous = voiceSessionsById.get(voiceKey);
      if (previous) {
        // A retained batch may have settled while microphone setup was pending.
        // Preserve it until the user explicitly retries or discards it.
        pipeline.abort();
        return;
      }
      voiceSessionsById.set(voiceKey, session);
      setVoiceRetainedSession(null);
      setRecordingSeconds(0);
      recordingTickerRef.current = setInterval(() => {
        setRecordingSeconds(Math.floor((Date.now() - startedAt) / 1000));
      }, 1000);
      setRecording(true);
    } catch {
      // getUserMedia may have handed us a live stream before capture setup
      // threw. Release it so the mic does not stay on.
      startingSession?.keeper?.forget();
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
  const readyAttachments = attachments.filter((a) => a.status === 'ready' || a.status === 'staged');
  const uploading = attachments.some((a) => a.status === 'uploading');
  // What a thumbnail preview pages through: the uploaded images staged right
  // here, in attachment order. Derived from the same list the chips render, so
  // the viewer's order is the row's order by construction.
  const stagedImageUrls = attachments
    .filter((a) => a.status === 'ready' && a.kind === 'image' && a.url)
    .map((a) => a.url);
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
    && !sendDisabled
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
  // Send and Stop trade places with a morph (docs/plans/web-ui-motion.md M2): the
  // control that arrives grows in where the other was. Once they have traded,
  // not as the composer first draws. Each control has its own key, so the one
  // that arrives is a new element: React would otherwise turn Stop's element
  // into Send, which keeps its finished animation and only shifts colours.
  const [controlsTraded, setControlsTraded] = useState(false);
  const [lastBusyControls, setLastBusyControls] = useState(busyControls);
  if (lastBusyControls !== busyControls) {
    setLastBusyControls(busyControls);
    setControlsTraded(true);
  }
  const tradedControl = controlsTraded ? 'motion-morph-in' : undefined;
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
            : voiceSlotHeld
              ? 'unavailable'
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

  // Hold the empty composer to talk (ui/design: composer/hold-desktop/*, composer/hold-mobile/*):
  // a press that stays put records, and releasing sends what was heard to this conversation,
  // with any staged files. Only while the box is empty and no tap dictation owns it; on a touch
  // screen also only while the keyboard is down, since a focused field's long press is the
  // system's (caret, selection, Paste). The mic button and the voice shortcut stay the tap
  // dictation into the draft.
  const [coarsePointer] = useState(
    () => typeof window !== 'undefined' && !!window.matchMedia?.('(pointer: coarse)').matches,
  );
  // What pressed last: a mouse holds by the ring and cancels by leaving; a finger or pen slides up.
  const [holdPointer, setHoldPointer] = useState<HoldPointer>(coarsePointer ? 'touch' : 'mouse');
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [inputFocused, setInputFocused] = useState(false);
  const [holdPoint, setHoldPoint] = useState<{ x: number; y: number } | null>(null);
  // A tap that should raise the keyboard: focused again in touchend, where iOS allows it.
  const tapFocusRef = useRef(false);
  // A mouse hold on a focused, empty box: if the take moves focus away, it comes back when the take ends.
  const holdRefocusRef = useRef(false);
  const holdOpen = (
    mediaEnabled
    && asrAvailable
    && !disabled
    && !sendDisabled
    && !voiceFlowActive
    && !hasComposerText
    && !uploading
  );
  const editorFocused = useCallback(() => {
    const active = document.activeElement;
    return active instanceof HTMLElement
      && Boolean(boxRef.current?.contains(active))
      && (active instanceof HTMLTextAreaElement || active.isContentEditable);
  }, []);
  const focusInput = useCallback(() => {
    if (useMentions) {
      // The DOM focus first, inside the gesture (iOS raises the keyboard only then); then the
      // editor's own, which syncs its selection.
      boxRef.current?.querySelector<HTMLElement>('[contenteditable="true"]')?.focus();
      mentionRef.current?.focus();
    } else {
      textareaRef.current?.focus();
    }
  }, [useMentions]);
  const canHoldNow = (pointerType: string) => (
    holdOpen && !pendingRef.current && (pointerType === 'mouse' || !editorFocused())
  );
  const hold = useHoldToTalk({
    send: async (text: string, metadata: VoiceMessageMetadata) => {
      // One send at a time, typed or spoken: the staged files go with exactly one message.
      if (pendingRef.current) return false;
      pendingRef.current = true;
      const sent = readyAttachments;
      let started: boolean | void;
      try {
        started = await onSend(text, sent, undefined, metadata);
      } catch {
        started = false;
      } finally {
        pendingRef.current = false;
      }
      if (started === false) {
        // Here, the take keeps the words (interrupted, send again). If the user has since left this
        // conversation, the take went with the composer: the words go to this session's draft,
        // as a refused typed message's do, unless a new draft was started.
        if (unmountedRef.current && !valueRef.current.trim()) {
          valueRef.current = text;
          onDraftChange?.(text);
        }
        return false;
      }
      // The staged files went with the message.
      const sentIds = new Set(sent.map((attachment) => attachment.localId));
      for (const attachment of sent) {
        attachmentFilesRef.current.delete(attachment.localId);
        if (stagedUrlsRef.current.delete(attachment.url)) URL.revokeObjectURL(attachment.url);
      }
      if (!unmountedRef.current) setAttachments((current) => current.filter((attachment) => !sentIds.has(attachment.localId)));
      return true;
    },
    canHold: canHoldNow,
    onTap: () => {
      // A click on the input has already placed the caret where it landed; focusing again would
      // rewrite that selection while the button is still down.
      if (editorFocused()) return;
      tapFocusRef.current = true;
      focusInput();
    },
    gesture: (pointerType) => (pointerType === 'mouse' ? MOUSE_HOLD_GESTURE : {}),
    bounds: () => boxRef.current?.getBoundingClientRect() ?? null,
    onSystemInterrupt: 'interrupt',
    context: () => readVoiceContext?.(),
    keep: () => (voiceKeepTarget ? { target: voiceKeepTarget } : null),
  });
  const holdBusy = hold.phase !== 'idle';
  const holdPhaseRef = useLatestRef(hold.phase);
  // A take owns the box from the press to its end: Enter and Send wait, so its staged files go
  // with one message only.
  const canSubmitNow = canSubmit && !holdBusy;
  // The touch styles that keep the system's long press (callout, selection, scroll) off a box a
  // hold may start on, kept through the press it took; never on a box with text or focus, which
  // the system owns.
  const holdTouchable = holdOpen && !inputFocused && hold.phase !== 'interrupted';
  // A touch press the hold took, decided at its pointerdown (which comes before its touchstart),
  // so the touch stays suppressed however soon the hold's own state lands.
  const touchHeldRef = useRef(false);
  useEffect(() => {
    if (hold.phase !== 'idle' || !holdRefocusRef.current) return;
    holdRefocusRef.current = false;
    if (!unmountedRef.current && !disabledRef.current && !editorFocused()) focusInput();
  }, [editorFocused, focusInput, hold.phase]);
  // Where a hold would record, the placeholder says so; while busy the caller's line still wins.
  const holdHint = holdOpen && hold.phase !== 'interrupted' && (!coarsePointer || !inputFocused);
  const idlePlaceholder = holdHint
    ? holdPlaceholder ?? (placeholder === undefined ? t('voice.composer.placeholder') : placeholder)
    : placeholder ?? t('chat.compose.placeholder');
  const holdTarget: VoiceTarget = voiceTarget ?? { name: '', title: t('voice.composer.toHere') };
  /** Spoken words into the end of the draft, to edit before sending. Reads the live draft: a
   *  kept take lands after an asynchronous claim, and edits made meanwhile stay. */
  const appendToDraft = (words: string) => {
    const current = valueRef.current;
    const next = current.trim() ? `${current.replace(/\s+$/, '')} ${words}` : words;
    valueRef.current = next;
    setHasText(true);
    if (useMentions) {
      mentionRef.current?.setText(next);
      mentionRef.current?.focus();
    } else {
      setValue(next);
      seedFocusRef.current = true;
    }
    draftEditedRef.current = true;
    onDraftChange?.(next);
  };
  const putHeardInDraft = () => {
    const heard = hold.text.trim();
    if (!heard) return;
    appendToDraft(heard);
    hold.discard();
  };
  // A kept take's words (ui/design: composer/pending-voice/*): into the draft unless voice owns
  // it right now, or sent as the spoken message they were meant to be. They land after an
  // asynchronous claim, so this reads the composer as it is then: one that is gone, or that a
  // take started on meanwhile, refuses them and the take stays kept.
  const insertKeptVoiceText = (words: string): boolean => {
    if (unmountedRef.current || disabledRef.current || voiceDraftLocked() || holdPhaseRef.current !== 'idle') return false;
    appendToDraft(words);
    return true;
  };
  const sendKeptVoiceText = async (words: string): Promise<boolean> => {
    // A send whose outcome is still unknown (`sendDisabled`) holds every other send back, this one too.
    if (pendingRef.current || disabledRef.current || sendDisabled) return false;
    pendingRef.current = true;
    try {
      return (await onSend(words, [], undefined, VOICE_MESSAGE_METADATA)) !== false;
    } catch {
      return false;
    } finally {
      pendingRef.current = false;
    }
  };
  const holdHandlers = mediaEnabled ? {
    onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => {
      touchHeldRef.current = false;
      // The controls in the box are buttons, never a hold.
      if (isInteractiveTarget(event.target)) return;
      // The same conditions the hook takes a press on.
      const takes = event.button === 0 && hold.phase === 'idle' && canHoldNow(event.pointerType) && !isVoiceCaptureActive();
      touchHeldRef.current = takes && event.pointerType !== 'mouse';
      if (takes) {
        const rect = event.currentTarget.getBoundingClientRect();
        setHoldPoint({ x: event.clientX - rect.left, y: event.clientY - rect.top });
        setHoldPointer(event.pointerType === 'mouse' ? 'mouse' : 'touch');
        holdRefocusRef.current = event.pointerType === 'mouse' && editorFocused();
      }
      hold.bind.onPointerDown(event);
    },
    // The click a held press ends with is spent; every other click is the box's own.
    onClick: hold.bind.onClick,
    onPointerMove: hold.bind.onPointerMove,
    onPointerUp: hold.bind.onPointerUp,
    onPointerCancel: hold.bind.onPointerCancel,
    onLostPointerCapture: hold.bind.onLostPointerCapture,
    onContextMenu: (event: React.MouseEvent<HTMLDivElement>) => {
      // Only a live press keeps the menu away; at rest a right-click is the system's (Paste).
      if (hold.phase !== 'idle' && hold.phase !== 'interrupted') hold.bind.onContextMenu(event);
    },
    onFocus: (event: React.FocusEvent<HTMLDivElement>) => {
      if (isEditableTarget(event.target)) setInputFocused(true);
    },
    onBlur: (event: React.FocusEvent<HTMLDivElement>) => {
      if (isEditableTarget(event.target)) setInputFocused(false);
    },
  } : {};
  const holdBoxStyle: React.CSSProperties | undefined = holdTouchable
    ? { touchAction: 'none', WebkitTouchCallout: 'none' }
    : undefined;
  // React's touch listeners are passive, so the one that keeps iOS from focusing, selecting, or
  // showing its menu on a press that may become a hold is attached here.
  useEffect(() => {
    const box = boxRef.current;
    if (!box || !mediaEnabled) return;
    const onTouchStart = (event: TouchEvent) => {
      // Only a press the hold took: one it refused keeps its tap (focus) and its menu.
      if (touchHeldRef.current) event.preventDefault();
    };
    const onTouchEnd = () => {
      touchHeldRef.current = false;
      if (!tapFocusRef.current) return;
      tapFocusRef.current = false;
      focusInput();
    };
    const onTouchCancel = () => {
      touchHeldRef.current = false;
    };
    box.addEventListener('touchstart', onTouchStart, { passive: false });
    box.addEventListener('touchend', onTouchEnd);
    box.addEventListener('touchcancel', onTouchCancel);
    return () => {
      box.removeEventListener('touchstart', onTouchStart);
      box.removeEventListener('touchend', onTouchEnd);
      box.removeEventListener('touchcancel', onTouchCancel);
    };
    // The box is a different element per layout.
  }, [focusInput, layout, mediaEnabled]);

  useLayoutEffect(() => {
    if (!focusNextVoiceControlRef.current) return;
    if (voiceControlMode === 'finish') {
      finishVoiceControlRef.current?.focus({ preventScroll: true });
      focusNextVoiceControlRef.current = false;
    } else if (voiceControlMode !== 'loading') {
      focusNextVoiceControlRef.current = false;
    }
  }, [voiceControlMode]);

  // Wait for MentionEditor's editable effect before returning keyboard focus.
  useEffect(() => {
    if (voiceDraftReadOnly || voiceEditorFocusReturnRef.current === null) return;
    const { target, previousFocus } = voiceEditorFocusReturnRef.current;
    voiceEditorFocusReturnRef.current = null;
    const activeElement = document.activeElement;
    if (
      !disabled
      && target.isConnected
      && (
        activeElement === previousFocus
        || activeElement === null
        || activeElement === document.body
        || !activeElement.isConnected
      )
    ) {
      if (useMentions) {
        // A DOM focus call is not enough for Lexical: it can leave the editor's
        // internal selection detached from the contenteditable after a voice
        // update completed while the editor was blurred. Restore through the
        // editor bridge so the caret committed by commitVoicePreview is synced
        // back to the DOM before the next Enter key.
        mentionRef.current?.focus();
      } else {
        target.focus({ preventScroll: true });
        if (target instanceof HTMLTextAreaElement && voiceTextareaCaretRef.current !== null) {
          const { start, end } = voiceTextareaCaretRef.current;
          target.setSelectionRange(start, end);
        }
      }
    }
  }, [disabled, useMentions, voiceDraftReadOnly]);

  const handleVoiceShortcut = useCallback((event: KeyboardEvent, allowStart: boolean): boolean => {
    if (
      holdBusy
      || !voiceShortcutAvailable
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
    if (voiceControlMode === 'finish') {
      const target = textareaRef.current
        ?? composerRootRef.current?.querySelector<HTMLElement>('[role="textbox"]');
      if (target && !inForegroundSurface(event.target as Element | null)) {
        // Finishing from anywhere on Chat should leave the draft ready for Enter,
        // unless the user moves focus elsewhere while transcription is pending.
        voiceEditorFocusReturnRef.current = { target, previousFocus: document.activeElement };
      }
      stopRecording();
    } else {
      const activeElement = document.activeElement;
      const editorFocus = (
        activeElement === textareaRef.current
        || (
          activeElement instanceof HTMLElement
          && activeElement.getAttribute('contenteditable') === 'true'
          && composerRootRef.current?.contains(activeElement)
        )
      ) ? activeElement as HTMLElement : null;
      voiceEditorFocusReturnRef.current = editorFocus
        ? { target: editorFocus, previousFocus: editorFocus }
        : null;
      // Keep the page's current focus when the shortcut started outside the
      // editor. Editor starts still move to Finish, then restore the caret.
      focusNextVoiceControlRef.current = editorFocus !== null;
      void startRecording(captureVoiceInsertion());
    }
    return true;
  }, [
    captureVoiceInsertion,
    holdBusy,
    startRecording,
    stopRecording,
    voiceControlMode,
    voiceInputShortcut,
    voiceShortcutAvailable,
  ]);

  // Home has no ChatPage shortcut owner. Scope its chord to the retained route
  // so Settings and foreground picker dialogs keep their own keyboard input.
  useRouteSurfaceWindowEvent('keydown', (event) => {
    handleVoiceShortcut(event, true);
  }, stageMedia);

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
    setDraft: (text: string) => {
      if (voiceDraftLocked()) return;
      draftEditedRef.current = true;
      valueRef.current = text;
      setHasText(text.trim().length > 0);
      if (useMentions) mentionRef.current?.setText(text);
      else setValue(text);
      onDraftChange?.(text);
      // Focus lands in the layout effect below, once the new text is in the DOM —
      // focusing here would put the caret at the pre-seed offset (0) instead of
      // after the text the user is meant to edit.
      seedFocusRef.current = true;
    },
    handleVoiceShortcut,
  }));

  useLayoutEffect(() => {
    if (!seedFocusRef.current) return;
    seedFocusRef.current = false;
    if (useMentions) {
      mentionRef.current?.focus();
    } else {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    }
    cancelSeedRevealRef.current();
    cancelSeedRevealRef.current = revealComposerAfterNativeKeyboardResize(composerRootRef.current);
  }, [value, useMentions]);

  useEffect(() => () => cancelSeedRevealRef.current(), []);

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
    draftEditedRef.current = true;
    valueRef.current = next;
    setValue(next);
    onDraftChange?.(next);
  };

  const submit = async () => {
    if (!canSubmitNow || pendingRef.current) return;
    // Mention path: the live text lives in valueRef (value isn't mirrored there).
    const submitted = (useMentions ? valueRef.current : value).trim();
    const sent = readyAttachments;
    const sentRefs = referencesRef.current;
    pendingRef.current = true;
    draftEditedRef.current = true;
    // Clear optimistically so the box can't be re-submitted and a slow send can't
    // wipe text typed in the meantime.
    valueRef.current = '';
    setValue('');
    setHasText(false);
    onDraftChange?.('');
    setAttachments([]);
    attachmentFilesRef.current.clear();
    referencesRef.current = [];
    // On a phone, sending ends the typing: the soft keyboard closes as the
    // message leaves, not when the server answers. A desktop, or a tablet on a
    // hardware keyboard, keeps the field focused for the next message.
    const closeKeyboard = isSoftKeyboardOpen();
    if (useMentions) mentionRef.current?.clear({ blur: closeKeyboard });
    if (closeKeyboard) dismissSoftKeyboard();
    try {
      // If the caller reports the send couldn't start (home no-project nudge),
      // restore the prompt + attachments for retry — unless the user typed anew.
      let started: boolean | void;
      try {
        started = await onSend(submitted, sent, useMentions ? sentRefs : undefined);
      } catch {
        showToast(t('chat.compose.sendFailed'), 'error');
        started = false;
      }
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
              // Chips re-resolve when re-picked; the content is never lost. The
              // keyboard closed with the send stays closed: the text comes back
              // without focus, and a tap on the field opens the keyboard again.
              referencesRef.current = sentRefs;
              mentionRef.current?.setText(submitted, { focus: !closeKeyboard });
            }
          }
          onDraftChange?.(submitted);
        }
      } else {
        void nativeHapticImpact('light');
        for (const attachment of sent) {
          if (stagedUrlsRef.current.delete(attachment.url)) URL.revokeObjectURL(attachment.url);
        }
      }
    } finally {
      pendingRef.current = false;
    }
  };

  // The media controls and the input itself, named so both box layouts below
  // compose the same controls instead of duplicating them. Attach stays in the
  // first idle slot and withdraws once a voice flow starts. The inline box keeps
  // Attach and Voice together on the left; the stacked box splits them — Attach
  // bottom-left, Voice beside Send.
  const stacked = layout === 'stacked';
  const attachControl = (
    <>
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
          className={stacked ? 'size-9 shrink-0' : 'h-9 w-7 shrink-0'}
        >
          <Plus className="size-4" />
        </Button>
      )}
    </>
  );
  const voiceControl = (
    <>
      {voiceControlMode === 'record' && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onPointerDown={() => {
            void prewarmVoiceCapture().catch(() => undefined);
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
          disabled={holdBusy || isVoiceControlDisabled(
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
      {voiceControlMode === 'unavailable' && (
        // Shown from the last known state, then refused by the live read: the
        // control keeps its place, disabled, until the composer next mounts.
        <Button
          type="button"
          variant="ghost"
          size="icon"
          disabled
          aria-label={t('chat.compose.voice')}
          title={t('chat.compose.voice')}
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
    </>
  );
  const mediaControls = mediaEnabled && (
    <div className="flex items-end">
      {attachControl}
      {voiceControl}
    </div>
  );

  const inputControl = (
    <>
        {useMentions ? (
          // Loaded with the chat page, so this does not suspend there; the
          // boundary only keeps a late editor from blanking the page around it.
          <Suspense fallback={null}>
          <DeferredMentionEditor
            ref={mentionRef}
            className="flex-1"
            placeholder={busyControls ? busyPlaceholder ?? t('chat.compose.placeholderBusy') : idlePlaceholder}
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
              if (!isDraftSeed) draftEditedRef.current = true;
              if (!isDraftSeed && !isVoicePreview) onDraftChange?.(text);
            }}
            onSubmit={submit}
          />
          </Suspense>
        ) : (
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => update(e.target.value)}
            onPaste={(event) => {
              const files = Array.from(event.clipboardData.files);
              if (mediaEnabled && files.length && !disabled && !voiceDraftReadOnly) {
                event.preventDefault();
                void uploadFiles(files);
              }
            }}
            onDragOver={(event) => { if (mediaEnabled && event.dataTransfer.types.includes('Files')) event.preventDefault(); }}
            onDrop={(event) => {
              if (!mediaEnabled || !event.dataTransfer.files.length) return;
              event.preventDefault();
              void uploadFiles(Array.from(event.dataTransfer.files));
            }}
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
            placeholder={busyControls ? busyPlaceholder ?? t('chat.compose.placeholderBusy') : idlePlaceholder}
            className="max-h-40 min-h-9 min-w-0 flex-1 resize-none bg-transparent py-2 text-[13px] leading-5 text-foreground outline-none placeholder:text-muted"
          />
        )}
    </>
  );

  // Send / Stop / Queue controls stay at the trailing edge of the action row.
  // Voice discard is rendered separately so the destructive action can stay at
  // the opposite edge from the primary voice control in the two-row layout.
  //
  // A running agent turn keeps its Stop escape hatch even during voice capture.
  // Queue/Send withdraw while capture owns the draft.
  const voiceDiscardControl = voiceDiscardAvailable && (
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
  );

  const sendControls = (
    <>
        {busyControls ? (
          <>
            {/* Sending while a turn runs is allowed — the backend enqueues it
                (202) instead of refusing (Enter does this too). Surface a visible
                affordance for it, but only when there's something to send: a cyan
                "queue" button left of Stop. Paper-plane + a small clock badge
                reads as "send, but later / into the queue". */}
            {!voiceCaptureActive && canSubmitNow && (
              <Button
                key="queue-send"
                type="button"
                variant="accent"
                size="icon"
                onClick={submit}
                aria-label={t(busySendsNow ? 'chat.compose.send' : 'chat.compose.queueSend')}
                title={t(busySendsNow ? 'chat.compose.send' : 'chat.compose.queueSend')}
                // It comes with text to send, beside Stop, the way Stop came.
                className="motion-morph-in size-9 shrink-0"
              >
                <span className="relative inline-flex">
                  <Send className="size-4" />
                  {!busySendsNow && (
                    <Clock
                      className="absolute -bottom-1 -right-1 size-2.5 rounded-full bg-surface-2"
                      strokeWidth={2.5}
                    />
                  )}
                </span>
              </Button>
            )}
            <Button
              key="stop"
              type="button"
              variant="destructive-soft"
              size="icon"
              onClick={onStop}
              disabled={!stopArmed}
              aria-label={t('chat.compose.stop')}
              className={clsx('size-9 shrink-0', tradedControl)}
            >
              <Square className="size-4" />
            </Button>
          </>
        ) : !voiceCaptureActive ? (
          <Button
            key="send"
            type="button"
            variant="default"
            size="icon"
            onClick={submit}
            disabled={!canSubmitNow}
            aria-label={t('chat.compose.send')}
            className={clsx('size-9 shrink-0', tradedControl)}
          >
            <Send className="size-4" />
          </Button>
        ) : null}
    </>
  );

  // While the microphone is open and while the take settles, the box stays above the card's halo,
  // which reaches down over it.
  const holdRaised = hold.phase === 'listening' || hold.phase === 'recording' || hold.phase === 'armed' || hold.phase === 'settling';
  const holdLayer = mediaEnabled && (
    <ComposerHoldCard
      hold={hold}
      pointer={holdPointer}
      to={holdTarget}
      attachments={readyAttachments.map((attachment) => attachment.name)}
      onEditFirst={putHeardInDraft}
      pending={{ target: voiceKeepTarget, onInsert: insertKeptVoiceText, onSend: sendKeptVoiceText }}
    />
  );
  const holdSurface = mediaEnabled && (
    <ComposerHoldSurface phase={hold.phase} pointer={holdPointer} progress={hold.progress} point={holdPoint} />
  );
  // The box a hold is pressed on, measured for leave-to-cancel; raised above the card's halo while held.
  const boxHoldProps = {
    ref: boxRef,
    ...holdHandlers,
    style: holdBoxStyle,
    'data-hold-to-talk': holdHint ? 'ready' : undefined,
  };

  return (
    <div
      ref={composerRootRef}
      data-composer-root
      className={cn('relative mx-auto flex w-full max-w-[1080px] flex-col gap-2', holdBusy && 'z-50', className)}
    >
      {holdLayer}
      {mediaEnabled && attachments.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {attachments.map((att) => (
            <div
              key={att.localId}
              className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 py-1 pl-1.5 pr-1 text-[12px]"
            >
              <AttachmentThumbnail att={att} gallery={stagedImageUrls} />
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
      {stacked ? (
        /* Two-row box: the input across the top, then the action row — Attach
           (or, mid-voice, Discard) on the left; the caller's actions, Voice and
           Send on the right. */
        <div
          {...boxHoldProps}
          className={cn(
            'relative flex w-full flex-col gap-1 rounded-2xl border border-border-strong bg-surface-2 p-2 shadow-[0_-4px_24px_-12px_rgba(0,0,0,0.5)]',
            holdRaised && 'z-50',
          )}
        >
          {holdSurface}
          <div className="flex w-full min-w-0 px-1.5">{inputControl}</div>
          <div className="flex w-full items-center justify-between gap-2">
            <div className="flex shrink-0 items-center gap-1">
              {voiceDiscardControl}
              {mediaEnabled && <div className="flex items-end">{attachControl}</div>}
            </div>
            <div className="flex min-w-0 items-center justify-end gap-1">
              {actions}
              {mediaEnabled && <div className="flex items-end">{voiceControl}</div>}
              {sendControls}
            </div>
          </div>
        </div>
      ) : (
        <div
          {...boxHoldProps}
          className={cn(
            'relative flex w-full items-end gap-1.5 rounded-2xl border border-border-strong bg-surface-2 py-2 pr-2 shadow-[0_-4px_24px_-12px_rgba(0,0,0,0.5)]',
            mediaEnabled ? 'pl-1.5' : 'pl-3.5',
            holdRaised && 'z-50',
          )}
        >
          {holdSurface}
          {mediaControls}
          {inputControl}
          {sendControls}
          {voiceDiscardControl}
        </div>
      )}
    </div>
  );
});
