import i18n from '@/i18n';
import { MainReplyBubble, PetComposerInput } from '@/pet/PetPage';
import { PetAvatar } from '@/pet/PetAvatar';
import '@/pet/pet-mini.css';
import { frames } from '../../frame';
import { mainBot, message, owner } from '../../shell/fixtures';

// The composer below Vibey's reply: its outer border lines up with the reply
// bubble's, and keeps that width when no reply is drawn above it.
const short = { zh: '好的，已经处理完了。', en: 'Done, all set.' };
const long = {
  zh: '空间还没回来。之前说一两小时会释放是估错了，抱歉。现在可用空间还需要核实。这里是隔离预览的示例文字，不代表当前磁盘状态。'.repeat(3),
  en: 'This is an isolated layout preview. A long explanation must remain readable without covering more of the desktop. '.repeat(5),
};
const noop = () => {};
const yes = async () => true;
const epoch = { binding: 'ses_preview', authorizationGeneration: 0, isCurrent: () => true };
const lang = () => (i18n.language.startsWith('zh') ? 'zh' : 'en');

const states = [
  { name: 'short-reply', reply: short, open: false, width: 460, height: 420 },
  { name: 'long-reply', reply: long, open: true, width: 460, height: 600 },
  { name: 'input-only', reply: null, open: false, width: 460, height: 320 },
  { name: 'narrow', reply: long, open: false, width: 280, height: 420 },
] as const;

const designFrames = frames(states.map((state) => ({
  name: state.name,
  title: `Mini composer width · ${state.name}`,
  width: state.width,
  height: state.height,
  status: 'shipped' as const,
  fixtures: { capabilities: owner, bots: [{ ...mainBot, display_name: '歪比' }] },
  render: () => <div className="pet-mini-stage">
    <div className="pet-mini-column">
      <div className="pet-mini-slot is-open"><div className="pet-mini-slot-inner"><div className="pet-mini-slot-body">
        {state.reply && <div className="pet-mini-bubble-over-input">
          <MainReplyBubble open={state.open} onToggle={noop} reducedMotion
            epoch={epoch} binding="ses_preview" voiceNeedsCloud={false} canChat
            exchange={{ user: null, results: [{ ...message('ses_preview', 'preview_reply', 'agent', state.reply[lang()], 1), read_at: '2026-11-11T00:00:00Z' }], unreadComplete: true, answersEarlierInput: false }}
            failure={null} running={false} queued={false} markRead={yes} onRead={noop}
            vaultRequestIds={[]} quickReplies={null} onChoose={yes} uncertain={false} onInspect={noop} />
        </div>}
        <PetComposerInput references={[]} value="" onValueChange={noop} onRemoveReference={noop} onSubmit={noop} onVoice={noop} sendDisabled />
      </div></div></div>
    </div>
    <div className="pet-mini-avatar-wrap"><PetAvatar pose="sleeping" /></div>
  </div>,
})));

export default designFrames;
