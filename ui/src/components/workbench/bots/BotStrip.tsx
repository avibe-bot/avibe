import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  DndContext,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
  type Modifier,
} from '@dnd-kit/core';
import { SortableContext, horizontalListSortingStrategy, sortableKeyboardCoordinates, useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { GripVertical, MessageCircle, Pencil, Plus, UserMinus } from 'lucide-react';
import clsx from 'clsx';

import { useApi, type InboxSession } from '../../../context/ApiContext';
import { useBots, type BotRuntimeStatus } from '../../../context/BotsContext';
import { useToast } from '../../../context/ToastContext';
import { useUnsavedChangesActionGuard } from '../../../context/useUnsavedChangesActionGuard';
import { botSessionPath, type Bot } from '@/lib/bots';
import { errorMessage } from '@/lib/errorMessage';
import { formatRelativeTime } from '@/lib/relativeTime';
import { Markdown } from '../../ui/markdown';
import { Popover, PopoverAnchor, PopoverContent } from '../../ui/popover';
import { BotAvatar } from '../../ui/bot-avatar';

// A slot is 50px wide (the 40px avatar, its 2px ring, and room for the name) with
// 4px between slots and 2px of padding at either end: the main Bot, two more,
// and "Add" fill the default 216px sidebar column exactly.
const SLOT_WIDTH = 50;
const SLOT_GAP = 4;
const STRIP_PADDING = 4;

const horizontalOnly: Modifier = ({ transform }) => ({ ...transform, y: 0 });

const unreadLabel = (count: number) => (count > 99 ? '99+' : String(count));

// The Bots strip (docs/plans/bots.md D5): the Owner's standing members as a row
// of 40px avatars in the Inbox's old place, the main Bot first. The open Bot has
// a mint ring, unread is a cyan count, a running Turn a pulsing mint dot. With
// room to spare "Add" follows the avatars; once they overflow they scroll
// sideways with a fading right edge and "Add" stays pinned at the right.
export const BotStrip: React.FC<{
  /** The Inbox's per-session unread map: a Bot's count is its Session's (D5). */
  unreadBySession: Record<string, number>;
}> = ({ unreadBySession }) => {
  const { t } = useTranslation();
  const { enabled, bots, statusBySession, displayName, openProfile, moveBot } = useBots();
  const [rowWidth, setRowWidth] = useState<number | null>(null);
  const [fade, setFade] = useState(false);
  const rowRef = useRef<HTMLDivElement | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const [draggingHandle, setDraggingHandle] = useState<string | null>(null);

  useLayoutEffect(() => {
    const row = rowRef.current;
    if (!row) return;
    const measure = () => setRowWidth(row.clientWidth);
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(row);
    return () => observer.disconnect();
  }, [enabled]);

  // Inline while every avatar and "Add" fit; pinned "Add" once they do not.
  const slots = bots.length + 1;
  const inline = rowWidth === null || slots * SLOT_WIDTH + (slots - 1) * SLOT_GAP + STRIP_PADDING <= rowWidth;

  const updateFade = useCallback(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    setFade(scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth - 2);
  }, []);
  useEffect(() => {
    updateFade();
  }, [updateFade, bots.length, inline, rowWidth]);

  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 300, tolerance: 8 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
      keyboardCodes: { start: ['Space'], cancel: ['Escape'], end: ['Space'] },
    }),
  );

  if (!enabled) return null;

  const main = bots.filter((bot) => bot.builtin);
  const others = bots.filter((bot) => !bot.builtin);
  const ids = others.map((bot) => bot.handle);
  const nameOf = (handle: string | number) => {
    const bot = others.find((item) => item.handle === handle);
    return bot ? displayName(bot) : '';
  };

  const slotProps = (bot: Bot) => ({
    bot,
    name: displayName(bot),
    unread: unreadBySession[bot.session_id] ?? 0,
    status: statusBySession[bot.session_id],
    suppressHover: draggingHandle !== null,
  });

  const addSlot = (
    <div className="flex w-[50px] shrink-0 flex-col items-center gap-1">
      <button
        type="button"
        onClick={() => openProfile({ kind: 'create' })}
        aria-label={t('bots.addLabel')}
        className="group rounded-full p-[2px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/60"
      >
        <span className="grid size-10 place-items-center rounded-full border border-dashed border-border-strong text-muted transition group-hover:border-mint group-hover:bg-mint-soft group-hover:text-mint-ink">
          <Plus className="size-4" />
        </span>
      </button>
      <span className="text-[11px] leading-tight text-muted">{t('bots.add')}</span>
    </div>
  );

  return (
    <section aria-label={t('bots.sectionLabel')} className="flex shrink-0 flex-col gap-1">
      <div className="flex h-4 items-center px-1">
        <span className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-muted">
          {t('bots.sectionLabel')}
        </span>
      </div>
      <div ref={rowRef} className="flex min-w-0 items-start">
        <div
          ref={scrollerRef}
          onScroll={updateFade}
          className={clsx(
            'flex min-w-0 items-start gap-1 overflow-x-auto px-0.5 pb-0.5 pt-1.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
            !inline && 'flex-1',
            !inline && fade && 'bot-strip-fade',
          )}
        >
          {main.map((bot) => <BotSlot key={bot.handle} {...slotProps(bot)} />)}
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            modifiers={[horizontalOnly]}
            accessibility={{
              screenReaderInstructions: { draggable: t('bots.reorderInstructions') },
              announcements: {
                onDragStart: ({ active }) => t('bots.reorderPickedUp', { name: nameOf(active.id) }),
                onDragOver: ({ active, over }) => over
                  ? t('bots.reorderPosition', { name: nameOf(active.id), position: ids.indexOf(String(over.id)) + 1, count: ids.length })
                  : undefined,
                onDragEnd: ({ active }) => t('bots.reorderDropped', { name: nameOf(active.id) }),
                onDragCancel: () => t('bots.reorderCancelled'),
              },
            }}
            onDragStart={({ active }) => setDraggingHandle(String(active.id))}
            onDragCancel={() => setDraggingHandle(null)}
            onDragEnd={({ active, over }) => {
              setDraggingHandle(null);
              const from = ids.indexOf(String(active.id));
              const to = ids.indexOf(String(over?.id));
              if (from >= 0 && to >= 0 && from !== to) void moveBot(String(active.id), to + 1);
            }}
          >
            <SortableContext items={ids} strategy={horizontalListSortingStrategy}>
              {others.map((bot) => (
                <SortableBotSlot key={bot.handle} sortable={others.length > 1} {...slotProps(bot)} />
              ))}
            </SortableContext>
          </DndContext>
          {inline && addSlot}
        </div>
        {!inline && <div className="shrink-0 pt-1.5">{addSlot}</div>}
      </div>
    </section>
  );
};

const RUNNING_DOT = (
  <span className="absolute -bottom-0.5 -right-0.5 grid size-3.5 place-items-center rounded-full bg-[var(--sidebar-background)]">
    <span className="relative size-2">
      <span className="absolute inset-0 animate-ping rounded-full bg-mint opacity-70 motion-reduce:animate-none" />
      <span className="relative block size-2 rounded-full bg-mint shadow-glow-dot-mint" />
    </span>
  </span>
);

type BotSlotProps = {
  bot: Bot;
  name: string;
  unread: number;
  status: BotRuntimeStatus | undefined;
  suppressHover: boolean;
};

type SlotDrag = Pick<ReturnType<typeof useSortable>, 'attributes' | 'listeners' | 'setNodeRef' | 'transform' | 'transition' | 'isDragging'>;

/** A Bot other than the main one: draggable along the strip once there are two. */
const SortableBotSlot: React.FC<BotSlotProps & { sortable: boolean }> = ({ sortable, ...props }) => {
  const drag = useSortable({ id: props.bot.handle, disabled: !sortable });
  return <BotSlot {...props} drag={sortable ? drag : undefined} />;
};

const BotSlot: React.FC<BotSlotProps & { drag?: SlotDrag }> = ({ bot, name, unread, status, suppressHover, drag }) => {
  const { t } = useTranslation();
  const api = useApi();
  const navigate = useNavigate();
  const location = useLocation();
  const { showToast } = useToast();
  const { openProfile, afterWrite } = useBots();
  const authorizeRouteAction = useUnsavedChangesActionGuard();
  const path = botSessionPath(bot);
  const selected = location.pathname === path;
  const running = status === 'running';
  const [hoverOpen, setHoverOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [latest, setLatest] = useState<InboxSession | null | undefined>(undefined);
  const openTimer = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);
  const isDragging = drag?.isDragging ?? false;
  const slotRef = useRef<HTMLDivElement | null>(null);
  const setDragNode = drag?.setNodeRef;
  const setSlotNode = useCallback((node: HTMLDivElement | null) => {
    slotRef.current = node;
    setDragNode?.(node);
  }, [setDragNode]);
  // The open Bot stays in view on a strip that scrolls sideways.
  useEffect(() => {
    if (selected) slotRef.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [selected]);

  const clearTimers = () => {
    if (openTimer.current !== null) window.clearTimeout(openTimer.current);
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    openTimer.current = null;
    closeTimer.current = null;
  };
  useEffect(() => clearTimers, []);
  // A drag anywhere on the strip takes the card away; its timers stop with it.
  useEffect(() => {
    if (suppressHover || isDragging) clearTimers();
  }, [suppressHover, isDragging]);

  const queueOpen = () => {
    if (suppressHover || menuOpen) return;
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    closeTimer.current = null;
    if (hoverOpen || openTimer.current !== null) return;
    openTimer.current = window.setTimeout(() => {
      openTimer.current = null;
      setHoverOpen(true);
    }, 350);
  };
  const queueClose = () => {
    if (openTimer.current !== null) window.clearTimeout(openTimer.current);
    openTimer.current = null;
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setHoverOpen(false);
    }, 180);
  };

  // The latest message is read when the card opens, so the strip itself costs
  // nothing per Bot. A Bot that has said nothing yet shows its role alone.
  useEffect(() => {
    if (!hoverOpen) return;
    let live = true;
    api
      .listInbox({ onlySession: bot.session_id, limit: 1, handleError: false })
      .then((feed) => {
        if (live) setLatest(feed.sessions.find((row) => row.session_id === bot.session_id) ?? null);
      })
      .catch(() => {
        if (live) setLatest(null);
      });
    return () => {
      live = false;
    };
  }, [api, bot.session_id, hoverOpen]);

  const open = () => {
    clearTimers();
    setHoverOpen(false);
    const authorization = authorizeRouteAction();
    if (!authorization) return;
    authorization.runNavigation(() => navigate(path));
  };

  const remove = async () => {
    setMenuOpen(false);
    try {
      await api.removeBot(bot.handle);
      showToast(t('bots.removed', { name }), 'success');
      if (selected) navigate('/');
    } catch (err) {
      showToast(errorMessage(err) || t('bots.removeFailed'), 'error');
    } finally {
      void afterWrite();
    }
  };

  const label = unread > 0 ? t('bots.slotLabelUnread', { name, count: unread }) : name;
  const showCard = hoverOpen && !menuOpen && !isDragging && !suppressHover;
  return (
    <Popover
      open={showCard || menuOpen}
      onOpenChange={(next) => {
        if (!next) {
          setHoverOpen(false);
          setMenuOpen(false);
        }
      }}
    >
      <PopoverAnchor asChild>
        <div
          ref={setSlotNode}
          data-bot-handle={bot.handle}
          className={clsx('flex w-[50px] shrink-0 flex-col items-center gap-1', isDragging && 'z-10 opacity-80')}
          style={drag ? { transform: CSS.Translate.toString(drag.transform), transition: drag.transition } : undefined}
          onMouseEnter={queueOpen}
          onMouseLeave={queueClose}
        >
          <button
            type="button"
            {...(drag?.attributes ?? {})}
            {...(drag?.listeners ?? {})}
            onClick={open}
            onContextMenu={(event) => {
              event.preventDefault();
              clearTimers();
              setHoverOpen(false);
              setMenuOpen(true);
            }}
            aria-label={label}
            aria-current={selected ? 'page' : undefined}
            className={clsx(
              'relative rounded-full p-[2px] transition-shadow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/60',
              selected ? 'shadow-[0_0_0_2px_var(--mint)]' : 'hover:shadow-[0_0_0_2px_var(--border-strong)]',
              drag && 'touch-none',
            )}
          >
            <BotAvatar avatar={bot.avatar} name={name} size={40} />
            {unread > 0 && (
              <span className="absolute -right-1.5 -top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-cyan px-1 font-mono text-[9px] font-bold text-accent-foreground shadow-glow-xs-cyan">
                {unreadLabel(unread)}
              </span>
            )}
            {running && RUNNING_DOT}
          </button>
          <span
            className={clsx(
              'w-full truncate text-center text-[11px] leading-tight',
              selected ? 'font-semibold text-foreground' : 'text-muted',
            )}
            title={name}
          >
            {name}
          </span>
        </div>
      </PopoverAnchor>
      {menuOpen ? (
        <PopoverContent
          side="bottom"
          align="start"
          sideOffset={6}
          role="menu"
          aria-label={t('bots.menu.label', { name })}
          data-settings-interaction-owner="true"
          onOpenAutoFocus={(event) => event.preventDefault()}
          className="flex w-[208px] flex-col gap-0.5 rounded-2xl border-border-strong bg-surface-2 p-1.5 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.6)]"
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setMenuOpen(false);
              openProfile({ kind: 'edit', handle: bot.handle });
            }}
            className="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px] text-foreground transition hover:bg-foreground/[0.05]"
          >
            <Pencil className="size-4 text-muted" />
            {t('bots.menu.editProfile')}
          </button>
          {!bot.builtin && (
            <>
              {/* Reordering is the drag itself; this row says how. */}
              <div className="flex items-center gap-2.5 px-2.5 py-2 text-[13px] text-muted">
                <GripVertical className="size-4" />
                {t('bots.menu.dragToReorder')}
              </div>
              <div className="my-1 h-px bg-border" />
              <button
                type="button"
                role="menuitem"
                onClick={() => void remove()}
                className="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px] text-destructive-ink transition hover:bg-destructive/[0.08]"
              >
                <UserMinus className="size-4" />
                {t('bots.menu.remove')}
              </button>
              <p className="px-2.5 pb-1.5 text-[11px] leading-relaxed text-muted">{t('bots.menu.removeNote')}</p>
            </>
          )}
        </PopoverContent>
      ) : (
        <PopoverContent
          side="right"
          align="start"
          sideOffset={10}
          data-settings-interaction-owner="true"
          onOpenAutoFocus={(event) => event.preventDefault()}
          onMouseEnter={queueOpen}
          onMouseLeave={queueClose}
          className="w-[248px] rounded-2xl border-border-strong bg-surface-2 p-0 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.6)]"
        >
          <button
            type="button"
            onClick={open}
            className="flex w-full flex-col gap-3 rounded-2xl p-3.5 text-left"
          >
            <span className="flex items-center gap-3">
              <BotAvatar avatar={bot.avatar} name={name} size={44} />
              <span className="min-w-0 flex-1 leading-tight">
                <span className="block truncate text-[14px] font-bold text-foreground">{name}</span>
                <span className="mt-0.5 block font-mono text-[11px] text-muted">{t('bots.handle', { handle: bot.handle })}</span>
              </span>
            </span>
            {bot.role && <span className="line-clamp-3 text-[12px] leading-relaxed text-muted">{bot.role}</span>}
            {(running || latest) && (
              <span className="block rounded-lg bg-foreground/[0.04] px-2.5 py-2 text-[12px] leading-relaxed text-foreground">
                <span className={clsx('mb-0.5 block text-[10px] font-semibold', running ? 'text-mint-ink' : 'text-muted')}>
                  {latest
                    ? t('bots.statusAt', {
                      status: running ? t('bots.running') : t('bots.latest'),
                      time: formatRelativeTime(latest.last_activity_at, t),
                    })
                    : running ? t('bots.running') : t('bots.latest')}
                </span>
                {latest?.preview_text && (
                  <span className="line-clamp-2 block">
                    <Markdown content={latest.preview_text} interactive={false} className="vr-markdown--preview" />
                  </span>
                )}
              </span>
            )}
            <span className="flex items-center gap-1.5 text-[12px] font-semibold text-cyan-ink">
              <MessageCircle className="size-3.5" />
              {t('bots.openConversation')}
            </span>
          </button>
        </PopoverContent>
      )}
    </Popover>
  );
};
