import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type Ref,
} from 'react';
import { LexicalComposer } from '@lexical/react/LexicalComposer';
import { PlainTextPlugin } from '@lexical/react/LexicalPlainTextPlugin';
import { ContentEditable } from '@lexical/react/LexicalContentEditable';
import { LexicalErrorBoundary } from '@lexical/react/LexicalErrorBoundary';
import { OnChangePlugin } from '@lexical/react/LexicalOnChangePlugin';
import { HistoryPlugin } from '@lexical/react/LexicalHistoryPlugin';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import {
  $createParagraphNode,
  $createRangeSelection,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isLineBreakNode,
  $isNodeSelection,
  $isRangeSelection,
  $isTextNode,
  $setSelection,
  COMMAND_PRIORITY_HIGH,
  HISTORIC_TAG,
  HISTORY_PUSH_TAG,
  KEY_ENTER_COMMAND,
  PASTE_COMMAND,
  type EditorState,
  type LexicalNode,
  type PointType,
} from 'lexical';
import {
  BeautifulMentionsPlugin,
  BeautifulMentionNode,
  $isBeautifulMentionNode,
  useBeautifulMentions,
  type BeautifulMentionsItem,
  type BeautifulMentionsMenuProps,
  type BeautifulMentionsMenuItemProps,
} from 'lexical-beautiful-mentions';

import { filesFromClipboard } from '../../lib/clipboardFiles';
import { isSoftKeyboardOpen, isTouchCapableDevice } from '../../lib/softKeyboard';
import { cn } from '../../lib/utils';
import { dedupeReferences, type MentionReference } from '../../lib/mentions';
import {
  applyVoiceInsertionWithSnapshot,
  voiceInsertionSnapshot,
  type VoiceInsertionSnapshot,
} from '../../lib/voiceCleanup';
import { isComposingKey } from '@/lib/imeComposition';
import { useLatestRef } from '@/lib/useLatestRef';

export type AgentSearchResult = {
  name: string;
  agent_id?: string | null;
  backend?: string | null;
  description?: string | null;
};
export type SessionSearchResult = { session_id: string; title?: string | null };

export interface MentionEditorHandle {
  focus: () => void;
  clear: () => void;
  /** Append free text at the end (voice transcript) without disturbing chips. */
  append: (text: string) => void;
  /** Capture the serialized draft and logical selection before a toolbar click
   *  moves DOM focus away from the editor. */
  captureSelection: () => VoiceInsertionSnapshot;
  /** Replace a captured logical range while preserving untouched mention chips.
   *  Returns false if the draft changed or the range can no longer be mapped. */
  replaceSelection: (snapshot: VoiceInsertionSnapshot, text: string) => boolean;
  /** Render a transient voice preview inside the captured range. Successive
   *  previews replace one another without persisting as draft edits. */
  showVoicePreview: (snapshot: VoiceInsertionSnapshot, text: string) => boolean;
  /** Replace the active preview, or the original captured range, with the final
   *  cleaned transcript as one ordinary draft edit. */
  commitVoicePreview: (snapshot: VoiceInsertionSnapshot, text: string) => boolean;
  /** Restore the rich editor state captured before the first voice preview.
   *  Refuses to restore if the user has changed the draft since that preview. */
  restoreVoicePreview: () => boolean;
  /** Replace the whole editor with plain text (restore on a failed send). */
  setText: (text: string) => void;
  /** Insert a mention chip at the current cursor — same node a picker-selected
   *  `@`/`#` yields — when triggered from outside the editor (e.g. "reference
   *  this session" in the sidebar). */
  insertMention: (
    trigger: string,
    value: string,
    data?: Record<string, string | number | boolean | null>,
  ) => void;
}

export interface MentionEditorProps {
  placeholder?: string;
  disabled?: boolean;
  autoFocus?: boolean;
  /** Seed once (saved draft). Markers in the seed restore as plain text in v1. */
  initialText?: string | null;
  className?: string;
  /** ``isDraftSeed`` flags the change produced by the mount-time draft seed
   *  (BootstrapPlugin), so callers can mirror the value without treating the
   *  restore as a user edit (e.g. skip re-persisting it as a draft). */
  onChange: (
    text: string,
    references: MentionReference[],
    isDraftSeed?: boolean,
    isVoicePreview?: boolean,
  ) => void;
  onSubmit: () => void;
  onSearchAgents: (query: string) => Promise<AgentSearchResult[]>;
  onSearchSessions: (query: string) => Promise<SessionSearchResult[]>;
  /** Pasting a file into the editor (clipboard screenshot or OS-copied file)
   *  hands it here instead of pasting as text. Unset → paste behaves as plain
   *  text (e.g. when the composer has no upload target). */
  onPasteFiles?: (files: File[]) => void;
}

// Per-trigger chip classes — styled in index.css to read like a Badge
// (success/mint for agents, info/cyan for sessions).
// Chip styling for the mention nodes inside the editor — Tailwind utilities that
// mirror Badge's success (agent) / info (session) variants.
const MENTION_THEME = {
  '@': 'rounded-full border border-mint/40 bg-mint-soft px-1.5 py-px font-medium text-mint-ink',
  '@Focused': 'ring-1 ring-mint/60',
  '#': 'rounded-full border border-cyan/40 bg-cyan-soft px-1.5 py-px font-medium text-cyan-ink',
  '#Focused': 'ring-1 ring-cyan/60',
};

// Walk a Lexical node into our marker text, collecting references as it goes.
function nodeToMarkerText(node: LexicalNode, refs: MentionReference[]): string {
  if ($isBeautifulMentionNode(node)) {
    const trigger = node.getTrigger();
    const value = node.getValue();
    const data = (node.getData() ?? {}) as Record<string, string | number | boolean | null>;
    if (trigger === '@') {
      // The marker terminates at the first `>`; a name containing `>` (or a
      // newline) would serialize to an ambiguous `@<a>b>`. Such names can't be
      // round-tripped, so fall back to plain text rather than a broken marker.
      // (searchAgents also filters these out — this is defense in depth.)
      if (/[>\n]/.test(value)) return `@${value}`;
      refs.push({
        kind: 'agent',
        name: value,
        agent_id: data.agentId != null ? String(data.agentId) : undefined,
        backend: data.backend != null ? String(data.backend) : undefined,
      });
      return `@<${value}>`;
    }
    if (trigger === '#') {
      const sessionId = data.sessionId != null ? String(data.sessionId) : value;
      refs.push({ kind: 'session', session_id: sessionId, title: value });
      return `#<${sessionId}>`;
    }
    return `${trigger}${value}`;
  }
  if ($isLineBreakNode(node)) return '\n';
  if ($isTextNode(node)) return node.getTextContent();
  if ($isElementNode(node)) {
    return node
      .getChildren()
      .map((child) => nodeToMarkerText(child, refs))
      .join('');
  }
  return '';
}

function serializeEditorState(state: EditorState): { text: string; references: MentionReference[] } {
  return state.read(serializeCurrentEditor);
}

function serializeCurrentEditor(): { text: string; references: MentionReference[] } {
  const refs: MentionReference[] = [];
  const blocks = $getRoot()
    .getChildren()
    .map((block) => nodeToMarkerText(block, refs));
  return { text: blocks.join('\n'), references: dedupeReferences(refs) };
}

const serializedNodeLength = (node: LexicalNode): number => nodeToMarkerText(node, []).length;

// Mention markers and their visible chip titles use different coordinate
// systems. Keep leaf ranges in marker offsets and use visible text only when a
// captured boundary touches a mention chip.
type SerializedVisibleSegment = {
  start: number;
  end: number;
  visibleText: string;
  mention: boolean;
};

function serializedVisibleSegments(): SerializedVisibleSegment[] {
  const segments: SerializedVisibleSegment[] = [];
  const visit = (node: LexicalNode, base: number, rootLevel = false) => {
    if ($isBeautifulMentionNode(node) || $isLineBreakNode(node) || $isTextNode(node)) {
      const visibleText = $isBeautifulMentionNode(node)
        ? node.getValue()
        : node.getTextContent();
      segments.push({
        start: base,
        end: base + serializedNodeLength(node),
        visibleText,
        mention: $isBeautifulMentionNode(node),
      });
      return;
    }
    if (!$isElementNode(node)) return;
    let offset = base;
    const children = node.getChildren();
    for (let index = 0; index < children.length; index += 1) {
      visit(children[index], offset);
      offset += serializedNodeLength(children[index]);
      if (rootLevel && index < children.length - 1) {
        segments.push({ start: offset, end: offset + 1, visibleText: '\n', mention: false });
        offset += 1;
      }
    }
  };
  visit($getRoot(), 0, true);
  return segments;
}

function visibleBoundaryAtOffset(target: number, side: 'left' | 'right'): string | undefined {
  const segments = serializedVisibleSegments();
  const ordered = side === 'left' ? [...segments].reverse() : segments;
  for (const segment of ordered) {
    if (side === 'left' && target <= segment.start) continue;
    if (side === 'right' && target >= segment.end) continue;
    if (!segment.mention) return undefined;
    if (segment.end - segment.start === segment.visibleText.length) {
      const offset = Math.max(0, Math.min(target - segment.start, segment.visibleText.length));
      return side === 'left'
        ? (Array.from(segment.visibleText.slice(0, offset)).at(-1) ?? '')
        : (Array.from(segment.visibleText.slice(offset))[0] ?? '');
    }
    const characters = Array.from(segment.visibleText);
    return side === 'left' ? (characters.at(-1) ?? '') : (characters[0] ?? '');
  }
  return undefined;
}

type SerializedPoint = {
  key: string;
  offset: number;
  type: 'text' | 'element';
};

function serializedOffsetForPoint(point: PointType): number | null {
  const root = $getRoot();
  const visit = (node: LexicalNode, base: number, rootLevel = false): number | null => {
    if (node.getKey() === point.key) {
      if (point.type === 'text' && $isTextNode(node)) {
        const serialized = nodeToMarkerText(node, []);
        if ($isBeautifulMentionNode(node)) {
          return base + (point.offset === 0 ? 0 : serialized.length);
        }
        return base + Math.min(point.offset, serialized.length);
      }
      if (point.type === 'element' && $isElementNode(node)) {
        const children = node.getChildren();
        let offset = base;
        const childCount = Math.min(point.offset, children.length);
        for (let index = 0; index < childCount; index += 1) {
          offset += serializedNodeLength(children[index]);
          if (rootLevel && index < children.length - 1) offset += 1;
        }
        return offset;
      }
    }
    if (!$isElementNode(node)) return null;
    const children = node.getChildren();
    let offset = base;
    for (let index = 0; index < children.length; index += 1) {
      const found = visit(children[index], offset);
      if (found !== null) return found;
      offset += serializedNodeLength(children[index]);
      if (rootLevel && index < children.length - 1) offset += 1;
    }
    return null;
  };
  return visit(root, 0, true);
}

function serializedPointAtOffset(target: number): SerializedPoint | null {
  const visit = (node: LexicalNode, base: number, rootLevel = false): SerializedPoint | null => {
    if ($isTextNode(node)) {
      const serialized = nodeToMarkerText(node, []);
      const textLength = node.getTextContentSize();
      if ($isBeautifulMentionNode(node)) {
        if (target === base) return { key: node.getKey(), offset: 0, type: 'text' };
        if (target === base + serialized.length) {
          return { key: node.getKey(), offset: textLength, type: 'text' };
        }
        return null;
      }
      if (target >= base && target <= base + serialized.length) {
        return { key: node.getKey(), offset: target - base, type: 'text' };
      }
      return null;
    }
    if (!$isElementNode(node)) return null;
    const children = node.getChildren();
    let offset = base;
    if (target === offset) {
      if (!rootLevel) return { key: node.getKey(), offset: 0, type: 'element' };
      const first = children[0];
      if (!first) return null;
      return $isElementNode(first)
        ? { key: first.getKey(), offset: 0, type: 'element' }
        : visit(first, offset);
    }
    for (let index = 0; index < children.length; index += 1) {
      const childEnd = offset + serializedNodeLength(children[index]);
      if (target > offset && target < childEnd) {
        return visit(children[index], offset);
      }
      if (target === childEnd) {
        if (rootLevel) {
          const child = children[index];
          return $isElementNode(child)
            ? { key: child.getKey(), offset: child.getChildrenSize(), type: 'element' }
            : visit(child, offset);
        }
        return { key: node.getKey(), offset: index + 1, type: 'element' };
      }
      offset = childEnd;
      if (rootLevel && index < children.length - 1) {
        offset += 1;
        if (target === offset) {
          const next = children[index + 1];
          return $isElementNode(next)
            ? { key: next.getKey(), offset: 0, type: 'element' }
            : visit(next, offset);
        }
      }
    }
    return null;
  };
  return visit($getRoot(), 0, true);
}

function serializedRangeForNodes(nodes: LexicalNode[]): { start: number; end: number } | null {
  const selectedKeys = new Set(nodes.map((node) => node.getKey()));
  let start = Number.POSITIVE_INFINITY;
  let end = Number.NEGATIVE_INFINITY;
  const visit = (node: LexicalNode, base: number, rootLevel = false) => {
    const length = serializedNodeLength(node);
    if (selectedKeys.has(node.getKey())) {
      start = Math.min(start, base);
      end = Math.max(end, base + length);
      return;
    }
    if (!$isElementNode(node)) return;
    let offset = base;
    const children = node.getChildren();
    for (let index = 0; index < children.length; index += 1) {
      visit(children[index], offset);
      offset += serializedNodeLength(children[index]);
      if (rootLevel && index < children.length - 1) offset += 1;
    }
  };
  visit($getRoot(), 0, true);
  return Number.isFinite(start) && Number.isFinite(end) ? { start, end } : null;
}

// Enter submits — except Shift+Enter (newline), mid-IME composition (CJK), while
// the on-screen keyboard is open (mobile: Enter = newline, send via button), or
// while the mention menu is open (Enter picks the highlighted suggestion).
function EnterSubmitPlugin({
  onSubmit,
  menuOpenRef,
}: {
  onSubmit: () => void;
  menuOpenRef: React.MutableRefObject<boolean>;
}) {
  const [editor] = useLexicalComposerContext();
  useEffect(
    () =>
      editor.registerCommand(
        KEY_ENTER_COMMAND,
        (event: KeyboardEvent | null) => {
          if (!event || event.shiftKey) return false;
          if (isComposingKey(event)) return false;
          if (menuOpenRef.current || isSoftKeyboardOpen()) return false;
          event.preventDefault();
          onSubmit();
          return true;
        },
        COMMAND_PRIORITY_HIGH,
      ),
    [editor, onSubmit, menuOpenRef],
  );
  return null;
}

function EditablePlugin({ disabled }: { disabled: boolean }) {
  const [editor] = useLexicalComposerContext();
  useEffect(() => {
    editor.setEditable(!disabled);
  }, [editor, disabled]);
  return null;
}

// Intercept a paste that carries files (a clipboard screenshot, or a file copied
// in the OS file manager) and hand it to the composer's uploader instead of
// letting Lexical insert it as text — the editor sibling of the `+` picker and
// chat-page drag-drop. Registered at HIGH priority so it runs before Lexical's
// own (LOW/EDITOR) paste handling: a files paste is consumed here (return true),
// while a plain text / rich-text paste carries no files and falls through
// (return false) to normal text pasting. Reads the callback through a ref so the
// command registers once per editor and never churns on the composer's renders.
function PasteFilesPlugin({ onPasteFiles }: { onPasteFiles?: (files: File[]) => void }) {
  const [editor] = useLexicalComposerContext();
  const handlerRef = useLatestRef(onPasteFiles);
  useEffect(
    () =>
      editor.registerCommand(
        PASTE_COMMAND,
        (event) => {
          const handler = handlerRef.current;
          if (!handler) return false;
          // PASTE_COMMAND can also fire for non-clipboard (input/keyboard) paste
          // triggers; only a ClipboardEvent carries files.
          const clipboardData = event instanceof ClipboardEvent ? event.clipboardData : null;
          const files = filesFromClipboard(clipboardData);
          if (files.length === 0) return false;
          event.preventDefault();
          handler(files);
          return true;
        },
        COMMAND_PRIORITY_HIGH,
      ),
    [editor],
  );
  return null;
}

// Update tag for the mount-time draft seed, so OnChange can tell "restored a
// saved draft" apart from a real user edit (restoring must not re-persist).
const DRAFT_SEED_TAG = 'avibe-draft-seed';
const VOICE_PREVIEW_TAG = 'avibe-voice-preview';

function BootstrapPlugin({
  autoFocus,
  initialText,
  bridgeRef,
}: {
  autoFocus: boolean;
  initialText?: string | null;
  bridgeRef: Ref<MentionEditorHandle>;
}) {
  const [editor] = useLexicalComposerContext();
  const { insertMention: insertBeautifulMention } = useBeautifulMentions();
  const seeded = useRef(false);
  const voicePreviewRef = useRef<{
    originalState: EditorState;
    insertion: VoiceInsertionSnapshot;
  } | null>(null);

  useImperativeHandle(
    bridgeRef,
    () => {
      const replaceCapturedSelection = (
        replacement: VoiceInsertionSnapshot,
        text: string,
        tag?: string | string[],
        insertion: VoiceInsertionSnapshot = replacement,
        keepEndVisible = false,
      ): VoiceInsertionSnapshot | null => {
        let inserted: VoiceInsertionSnapshot | null = null;
        editor.update(() => {
          const current = serializeCurrentEditor().text;
          const result = applyVoiceInsertionWithSnapshot(
            current,
            replacement,
            text,
            insertion,
          );
          if (result === null) return;
          const start = serializedPointAtOffset(replacement.start);
          const end = serializedPointAtOffset(replacement.end);
          if (!start || !end) return;
          const selection = $createRangeSelection();
          selection.anchor.set(start.key, start.offset, start.type);
          selection.focus.set(end.key, end.offset, end.type);
          $setSelection(selection);
          selection.insertText(result.insertion);
          inserted = result.snapshot;
        }, {
          tag,
          onUpdate: () => {
            if (!keepEndVisible || inserted === null || inserted.end !== inserted.text.length) return;
            const root = editor.getRootElement();
            if (root !== null) root.scrollTop = root.scrollHeight;
          },
        });
        return inserted;
      };

      return {
        focus: () => editor.focus(),
        clear: () => {
          voicePreviewRef.current = null;
          editor.update(() => {
            const root = $getRoot();
            root.clear();
            root.append($createParagraphNode());
          });
        },
        append: (text: string) =>
          editor.update(() => {
            const root = $getRoot();
            const selection = root.selectEnd();
            const prefix = root.getTextContent().length > 0 ? ' ' : '';
            selection.insertText(`${prefix}${text}`);
          }),
        captureSelection: () => editor.getEditorState().read(() => {
          const { text } = serializeCurrentEditor();
          const snapshotAt = (start: number, end: number) => voiceInsertionSnapshot(
            text,
            start,
            end,
            {
              left: visibleBoundaryAtOffset(start, 'left'),
              right: visibleBoundaryAtOffset(end, 'right'),
            },
          );
          const selection = $getSelection();
          if ($isNodeSelection(selection)) {
            const range = serializedRangeForNodes(selection.getNodes());
            if (range) return snapshotAt(range.start, range.end);
          }
          if (!$isRangeSelection(selection)) {
            return snapshotAt(text.length, text.length);
          }
          const anchor = serializedOffsetForPoint(selection.anchor);
          const focus = serializedOffsetForPoint(selection.focus);
          if (anchor === null || focus === null) {
            return snapshotAt(text.length, text.length);
          }
          return snapshotAt(Math.min(anchor, focus), Math.max(anchor, focus));
        }),
        replaceSelection: (snapshot, text) => replaceCapturedSelection(snapshot, text) !== null,
        showVoicePreview: (snapshot, text) => {
          if (!text.trim()) return voicePreviewRef.current !== null;
          const active = voicePreviewRef.current;
          const originalState = active?.originalState ?? editor.getEditorState();
          const insertion = replaceCapturedSelection(
            active?.insertion ?? snapshot,
            text,
            [VOICE_PREVIEW_TAG, HISTORIC_TAG],
            snapshot,
            true,
          );
          if (insertion === null) {
            voicePreviewRef.current = null;
            return false;
          }
          voicePreviewRef.current = { originalState, insertion };
          return true;
        },
        commitVoicePreview: (snapshot, text) => {
          const active = voicePreviewRef.current;
          const inserted = replaceCapturedSelection(
            active?.insertion ?? snapshot,
            text,
            HISTORY_PUSH_TAG,
            snapshot,
            true,
          );
          if (inserted !== null) voicePreviewRef.current = null;
          return inserted !== null;
        },
        restoreVoicePreview: () => {
          const active = voicePreviewRef.current;
          if (active === null) return false;
          const current = editor.getEditorState().read(() => serializeCurrentEditor().text);
          voicePreviewRef.current = null;
          if (current !== active.insertion.text) return false;
          editor.setEditorState(active.originalState, { tag: HISTORIC_TAG });
          return true;
        },
        setText: (text: string) => {
          voicePreviewRef.current = null;
          editor.update(() => {
            const root = $getRoot();
            root.clear();
            const paragraph = $createParagraphNode();
            root.append(paragraph);
            if (text) paragraph.selectStart().insertText(text);
          });
        },
        // Insert at the live selection (Lexical keeps it across DOM blur), then
        // focus so the user can keep typing. Produces the same node a typed
        // `#`/`@` pick does, so serialization (#<id> + references) is identical.
        insertMention: (trigger, value, data) =>
          insertBeautifulMention({ trigger, value, data: data ?? {}, focus: true }),
      };
    },
    [editor, insertBeautifulMention],
  );

  useEffect(() => {
    if (seeded.current) return;
    seeded.current = true;
    const raw = initialText ?? '';
    if (!raw.trim()) {
      if (autoFocus && !isTouchCapableDevice()) editor.focus();
      return;
    }
    editor.update(
      () => {
        const root = $getRoot();
        root.clear();
        const paragraph = $createParagraphNode();
        root.append(paragraph);
        // v1: a restored draft seeds as plain text (markers render raw until
        // re-picked); the content is lossless for sending. Insert the raw draft so
        // intentional leading/trailing whitespace survives the round-trip.
        paragraph.selectStart().insertText(raw);
      },
      // Tagged so OnChange reports this as a seed, not a user edit.
      { tag: DRAFT_SEED_TAG },
    );
    if (autoFocus && !isTouchCapableDevice()) editor.focus();
    // Only on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return null;
}

// After a mention is inserted at the very END of the input, beautiful-mentions adds
// no trailing space and leaves the caret as a node-selection on the decorator chip
// (it only spaces when content follows). beautiful-mentions calls onMenuItemSelect
// BEFORE its insertion runs, so instead we react via a mutation listener that fires
// AFTER the node is inserted: append a trailing space and move the caret past it.
// Only the trailing-mention case is touched; mid-text insertions already get a space.
function MentionCaretFixPlugin() {
  const [editor] = useLexicalComposerContext();
  useEffect(
    () =>
      editor.registerMutationListener(
        BeautifulMentionNode,
        (mutations) => {
          let created = false;
          for (const mutation of mutations.values()) {
            if (mutation === 'created') created = true;
          }
          if (!created) return;
          editor.update(() => {
            const last = $getRoot().getLastDescendant();
            if ($isBeautifulMentionNode(last) && last.getNextSibling() === null) {
              const space = $createTextNode(' ');
              last.insertAfter(space);
              space.select(1, 1);
            }
          });
        },
        { skipInitialization: true },
      ),
    [editor],
  );
  return null;
}

// Voice/dictation IMEs (e.g. Typeless) "replace" a selection by deleting it and
// re-inserting the full multi-paragraph result as ONE `insertText` whose `data`
// carries newlines. In PlainText Lexical, when the editor isn't cleanly empty
// Lexical can decline to control that insert and let the browser perform it — the
// browser splits the text into block elements, and PlainText's DOM→state
// reconciliation then keeps only the FIRST block, silently dropping every later
// paragraph (intermittently, depending on the IME's delete/re-insert race).
// Capture the multi-line `insertText` before Lexical/the browser handle it and
// insert it ourselves via insertRawText (proper LineBreakNodes), which is
// lossless. Registered on the document in the capture phase so it runs ahead of
// Lexical's own root beforeinput listener; stopPropagation prevents a double
// insert.
//
// Three guards keep us from ever swallowing input we wouldn't faithfully re-insert:
//   • only `insertText` — NOT `insertReplacementText`, whose getTargetRanges() may
//     differ from the selection (autocorrect/spellcheck), which would land the
//     text at the wrong spot;
//   • only when `event.cancelable` — a non-cancelable beforeinput ignores
//     preventDefault, so taking over would double-insert alongside the native edit;
//   • only when a RangeSelection can receive it — otherwise (e.g. a node-selected
//     mention chip) defer to the normal path so the text is never dropped.
function MultilineInsertFixPlugin() {
  const [editor] = useLexicalComposerContext();
  useEffect(() => {
    const onBeforeInput = (event: InputEvent) => {
      if (event.inputType !== 'insertText' || !event.cancelable) return;
      const text = event.data ?? '';
      if (!text.includes('\n')) return;
      const root = editor.getRootElement();
      const target = event.target as Node | null;
      if (!root || !target || !(root === target || root.contains(target))) return;
      // Confirm a range selection can take the insert BEFORE cancelling the default,
      // so non-range selections fall through to the normal path instead of dropping.
      if (!editor.getEditorState().read(() => $isRangeSelection($getSelection()))) return;
      event.preventDefault();
      event.stopPropagation();
      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) selection.insertRawText(text);
      });
    };
    document.addEventListener('beforeinput', onBeforeInput, true);
    return () => document.removeEventListener('beforeinput', onBeforeInput, true);
  }, [editor]);
  return null;
}

const MentionMenu = forwardRef<HTMLUListElement, BeautifulMentionsMenuProps>(
  ({ loading: _loading, children, ...props }, ref) => (
    // Always open ABOVE the caret as an out-of-flow overlay — the chat composer is
    // pinned to the viewport bottom. Pinning the BOTTOM edge to the anchor (caret)
    // means the list grows UPWARD as async results load, with no reposition or
    // flicker. `!important` beats the inline `top` LexicalTypeaheadMenuPlugin writes
    // on the menu element for measurement. We deliberately do NOT measure room to
    // "drop down": on mobile that re-ran on every async-results re-render and
    // intermittently flipped the list below the input (visualViewport vs layout-rect
    // timing). The plugin's own flip needs a 1–2 item list AND a tall multiline
    // composer to trigger, which our 5–8 item menus effectively never hit.
    <ul
      ref={ref}
      data-mention-picker
      className="absolute left-0 z-50 mb-4 !bottom-full !top-auto max-h-64 min-w-[15rem] list-none overflow-y-auto overflow-x-hidden rounded-md border border-border bg-panel p-1 text-text shadow-md"
      {...props}
    >
      {children}
    </ul>
  ),
);
MentionMenu.displayName = 'MentionMenu';

const MentionMenuItem = forwardRef<HTMLLIElement, BeautifulMentionsMenuItemProps>(
  ({ selected, item, itemValue: _itemValue, label: _label, ...props }, ref) => {
    const data = (item.data ?? {}) as Record<string, string | number | boolean | null>;
    // Agents show their backend as a secondary hint; sessions show only the title.
    const secondary = item.trigger === '@' && data.backend != null ? String(data.backend) : '';
    return (
      <li
        ref={ref}
        className={cn(
          'flex cursor-pointer select-none items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none',
          selected ? 'bg-accent/10 text-accent-ink' : 'text-text',
        )}
        {...props}
      >
        <span className="shrink-0 text-muted">{item.trigger}</span>
        <span className="truncate">{item.value}</span>
        {secondary ? <span className="ml-auto shrink-0 text-xs text-muted">{secondary}</span> : null}
      </li>
    );
  },
);
MentionMenuItem.displayName = 'MentionMenuItem';

// A Lexical-backed text input with `@` (agent) / `#` (session) inline-chip
// mentions. Owns only the editor; the surrounding composer shell (send button,
// attachment chips, voice) stays in Composer.
export const MentionEditor = forwardRef<MentionEditorHandle, MentionEditorProps>(function MentionEditor(
  {
    placeholder,
    disabled = false,
    autoFocus = false,
    initialText = null,
    className,
    onChange,
    onSubmit,
    onSearchAgents,
    onSearchSessions,
    onPasteFiles,
  },
  ref,
) {
  const menuOpenRef = useRef(false);

  const handleChange = useCallback(
    (state: EditorState, _editor: unknown, tags: Set<string>) => {
      const { text, references } = serializeEditorState(state);
      onChange(
        text,
        references,
        tags.has(DRAFT_SEED_TAG),
        tags.has(VOICE_PREVIEW_TAG),
      );
    },
    [onChange],
  );

  const onSearch = useCallback(
    async (trigger: string, queryString?: string | null): Promise<BeautifulMentionsItem[]> => {
      const query = (queryString ?? '').trim();
      if (trigger === '@') {
        const agents = await onSearchAgents(query);
        return agents.map((a) => ({
          value: a.name,
          agentId: a.agent_id ?? null,
          backend: a.backend ?? null,
        }));
      }
      if (trigger === '#') {
        const sessions = await onSearchSessions(query);
        return sessions.map((s) => ({
          value: s.title && s.title.trim() ? s.title : s.session_id,
          sessionId: s.session_id,
        }));
      }
      return [];
    },
    [onSearchAgents, onSearchSessions],
  );

  // Lexical reads `initialConfig` once, at mount; a fresh object on later
  // renders would be ignored anyway. A lazy `useState` initializer is the
  // render-safe way to spell "compute once per instance" — `useRef({…}).current`
  // rebuilt the object on every render only to throw it away, and reading
  // `.current` during render is what `react-hooks/refs` warns about.
  const [initialConfig] = useState(() => ({
    namespace: 'avibe-mention-composer',
    theme: { beautifulMentions: MENTION_THEME },
    nodes: [BeautifulMentionNode],
    editable: !disabled,
    onError: (error: Error) => {
      // Surface in dev; never throw out of the editor and wipe the box.
      console.error('[MentionEditor]', error);
    },
  }));

  return (
    <div className={cn('relative', className)}>
      <LexicalComposer initialConfig={initialConfig}>
        <PlainTextPlugin
          contentEditable={
            <ContentEditable
              className="max-h-40 min-h-9 w-full overflow-y-auto whitespace-pre-wrap break-words bg-transparent py-2 text-[13px] leading-5 text-foreground outline-none"
              aria-label={placeholder}
              spellCheck
            />
          }
          placeholder={
            <div className="pointer-events-none absolute left-0 top-2 select-none text-[13px] leading-5 text-muted">
              {placeholder}
            </div>
          }
          ErrorBoundary={LexicalErrorBoundary}
        />
        <HistoryPlugin />
        <OnChangePlugin onChange={handleChange} ignoreSelectionChange />
        <BeautifulMentionsPlugin
          triggers={['@', '#']}
          onSearch={onSearch}
          searchDelay={150}
          menuItemLimit={8}
          // Allow `@`/`#` after a word boundary without a leading space (including
          // CJK, which has no inter-word spaces) but NOT inside a Latin word / number
          // / `_` token, so ordinary text like `name@host` or `C#` doesn't open the
          // picker mid-token (Codex P2).
          preTriggerChars={'[^\\sA-Za-z0-9_]'}
          // Only Agents/Sessions returned by onSearch may become chips — no
          // user-created (unresolved) mentions (the picker-selected-only contract).
          creatable={false}
          insertOnBlur={false}
          menuComponent={MentionMenu}
          menuItemComponent={MentionMenuItem}
          menuAnchorClassName="z-50"
          onMenuOpen={() => {
            menuOpenRef.current = true;
          }}
          onMenuClose={() => {
            menuOpenRef.current = false;
          }}
        />
        <EnterSubmitPlugin onSubmit={onSubmit} menuOpenRef={menuOpenRef} />
        <EditablePlugin disabled={disabled} />
        <PasteFilesPlugin onPasteFiles={onPasteFiles} />
        <MultilineInsertFixPlugin />
        <BootstrapPlugin autoFocus={autoFocus} initialText={initialText} bridgeRef={ref} />
        <MentionCaretFixPlugin />
      </LexicalComposer>
    </div>
  );
});
