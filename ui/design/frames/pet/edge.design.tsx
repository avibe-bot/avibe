import type { CSSProperties } from 'react';
import i18n from '@/i18n';
import { ExpandedCards, MainReplyBubble, PetComposerInput } from '@/pet/PetPage';
import { PetAvatar } from '@/pet/PetAvatar';
import '@/pet/pet-mini.css';
import { frames } from '../../frame';
import { mainBot, message, owner } from '../../shell/fixtures';

// Vibey dragged against a display's side (decision 2026-11-20): it rests 12pt
// from the edge, the 500pt window passes the edge with its transparent side,
// and the shell moves the column inward (column_offset ±89) so the cards, the
// reply and the composer stay whole on the display, 18pt from its edge. The
// bubble's tail still points at Vibey. Left edge: the display starts 180.5pt
// into the window; right edge: it ends 180.5pt before the window's right.
const reply = {
  zh: '好的，已经处理完了。空间清出来了，可以继续下一步。',
  en: 'Done, all set. The space is back, so the next step can go ahead.',
};
const noop = () => {};
const yes = async () => true;
const epoch = { binding: 'ses_preview', authorizationGeneration: 0, isCurrent: () => true };
const t = (key: string, options?: Record<string, unknown>) => String(i18n.t(key as never, options as never));
const lang = () => (i18n.language.startsWith('zh') ? 'zh' : 'en');
const cards = ['running', 'replied'].map((state, index) => ({
  id: state, sessionId: `ses_preview_${index}`,
  title: state === 'running' ? 'Weekly report draft / 周报草稿' : 'Disk cleanup / 磁盘清理',
  eyebrow: 'avibe · codex',
  status: state === 'running' ? 'Running / 运行中' : 'Replied / 已回复',
  running: state === 'running', needsInput: false, hasUnread: state === 'replied',
  canStop: state === 'running', canOpen: true, vaultRequestIds: [],
}));

const states = [
  { name: 'edge-left', offset: 89, running: false },
  { name: 'edge-right', offset: -89, running: false },
  { name: 'edge-left-thinking', offset: 89, running: true },
] as const;

const designFrames = frames(states.map((state) => ({
  name: state.name,
  title: `Mini against the display's ${state.offset > 0 ? 'left' : 'right'} side · column_offset ${state.offset}${state.running ? ' · thinking' : ''}`,
  width: 500,
  height: 600,
  status: 'shipped' as const,
  fixtures: { capabilities: owner, bots: [{ ...mainBot, display_name: '歪比' }] },
  render: () => <div className="pet-mini-stage" style={{ '--pet-column-offset': `${state.offset}px` } as CSSProperties}>
    <div className="pet-mini-column">
      <div className="pet-mini-cards">
        <ExpandedCards cards={cards} leaving={[]} onReference={noop} onDismiss={noop} stops={{}} onStop={noop} t={t} />
      </div>
      <div className="pet-mini-slot is-open"><div className="pet-mini-slot-inner"><div className="pet-mini-slot-body">
        <div className="pet-mini-bubble-over-input">
          <MainReplyBubble open={false} onToggle={noop} reducedMotion
            epoch={epoch} binding="ses_preview" voiceNeedsCloud={false} canChat
            exchange={{ user: null, results: state.running ? [] : [{ ...message('ses_preview', 'preview_reply', 'agent', reply[lang()], 1), read_at: '2026-11-11T00:00:00Z' }], unreadComplete: true, answersEarlierInput: false }}
            failure={null} running={state.running} queued={false} markRead={yes} onRead={noop}
            vaultRequestIds={[]} quickReplies={null} onChoose={yes} uncertain={false} onInspect={noop} />
        </div>
        <PetComposerInput references={[]} value="" onValueChange={noop} onRemoveReference={noop} onSubmit={noop} onVoice={noop} sendDisabled />
      </div></div></div>
    </div>
    <div className="pet-mini-avatar-wrap"><PetAvatar pose="sleeping" /></div>
  </div>,
})));

export default designFrames;
