import i18n from '@/i18n';
import { CollapsedCards, ExpandedCards, MainReplyBubble } from '@/pet/PetPage';
import { PetAvatar } from '@/pet/PetAvatar';
import '@/pet/pet-mini.css';
import { frames } from '../../frame';
import { mainBot, message, owner } from '../../shell/fixtures';

const chinese = '空间还没回来。之前说一两小时会释放是估错了，抱歉。现在可用空间还需要核实。这里是隔离预览的示例文字，不代表当前磁盘状态。'.repeat(4);
const english = 'This is an isolated layout preview. A long explanation must remain readable without covering more of the desktop. '.repeat(8);
const code = 'Example output:\n\n```text\n' + 'very_long_path_segment_'.repeat(15) + '\n```\n\n' + chinese;
const noop = () => {};
const yes = async () => true;
const epoch = { binding: 'ses_preview', authorizationGeneration: 0, isCurrent: () => true };

const t = (key: string, options?: Record<string, unknown>) => String(i18n.t(key as never, options as never));
const cards = ['authorization', 'running', 'replied'].map((state, index) => ({
  id: state, sessionId: `ses_preview_${index}`,
  title: index === 1 ? 'LongUnbrokenSessionTitleWithManyCharacters'.repeat(4) : '检查长期项目中的授权与进度'.repeat(6),
  eyebrow: index === 1 ? 'LongProjectNameWithNoBreaks'.repeat(4) : '很长的项目名称 · codex',
  status: state === 'authorization' ? 'Needs authorization / 等待授权' : state === 'running' ? 'Running / 运行中' : 'Replied / 已回复',
  running: state === 'running', needsInput: state === 'authorization', hasUnread: state === 'replied',
  canStop: state === 'running', canOpen: true, vaultRequestIds: state === 'authorization' ? ['preview_request'] : [],
}));

const designFrames = frames(['chinese', 'english', 'long-word', 'code', 'actions', 'approval', 'activity-single', 'activity-stack', 'activity-expanded', 'activity-narrow'].map((kind) => {
  const result = { ...message('ses_preview', 'preview_reply', 'agent', kind === 'long-word' ? 'LongUnbrokenWord'.repeat(25) : kind === 'english' ? english : kind === 'code' ? code : chinese, 1), read_at: '2026-11-11T00:00:00Z' };
  const options = ['查看详细结果', '先保留当前方案', 'Show more details'];
  return {
    name: kind,
    title: `Mini reply · ${kind} · 280px cap`,
    width: kind === 'activity-narrow' ? 280 : 460, height: kind.startsWith('activity-') ? 600 : kind === 'code' || kind === 'actions' || kind === 'approval' ? 600 : 320,
    status: 'shipped' as const,
    fixtures: { capabilities: owner, bots: [{ ...mainBot, display_name: '歪比' }] },
    render: () => <div className="pet-mini-stage">
      <div className="pet-mini-column">
        {kind.startsWith('activity-') && <div className="pet-mini-cards max-w-79">
          {kind === 'activity-stack' ? <CollapsedCards cards={cards} edge="bottom" onOpen={noop} t={t} /> :
            <ExpandedCards natural={kind === 'activity-single'} cards={kind === 'activity-single' ? cards.slice(0, 1) : cards}
              leaving={[]} onReference={noop} onDismiss={noop} stops={{}} onStop={noop} t={t} />}
        </div>}
        <div className="pet-mini-slot is-open"><div className="pet-mini-slot-inner"><div className="pet-mini-slot-body">
        <div className="w-full">
          <MainReplyBubble open={kind === 'code' || kind === 'actions' || kind === 'approval'} onToggle={noop} reducedMotion
            epoch={epoch} binding="ses_preview" voiceNeedsCloud={false} canChat
            exchange={{ user: null, results: [result], unreadComplete: true, answersEarlierInput: false }}
            failure={null} running={false} queued={false} markRead={yes} onRead={noop}
            vaultRequestIds={kind === 'approval' ? ['preview_request'] : []} quickReplies={kind === 'actions' ? { message: result, options } : null}
            onChoose={yes} uncertain={false} onInspect={noop} />
        </div>
      </div></div></div></div>
      <div className="pet-mini-avatar-wrap"><PetAvatar pose="sleeping" /></div>
    </div>,
  };
}));

export default designFrames;
