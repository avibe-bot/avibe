// The Live Activity states the I5 frames draw (ui/design: ios/live-activity/*), in the frame's language.
import { useDraft } from '../../kit/draft';
import { IslandCompact, IslandExpanded, IslandMinimal, LiveActivityCard, type TurnActivity } from '../../kit/live-activity';

/** The states in the frame's language (kit copy is draft copy, written inline in both). */
function useActivities() {
  const t = useDraft();
  const agent = t('首席安全官', 'Security Lead');
  const title = t('本周新增依赖的高危漏洞审计', 'Audit new dependencies for high-severity CVEs');
  const base = { agent, title };
  return {
    working: { ...base, phase: 'working', elapsed: '42:10' },
    needsYou: { ...base, phase: 'needsYou', elapsed: '43:02' },
    done: { ...base, phase: 'done', elapsed: '', took: t('47 分钟', '47 min') },
    failed: { ...base, phase: 'failed', elapsed: '' },
    stopped: { ...base, phase: 'stopped', elapsed: '', took: t('12 分钟', '12 min') },
    stale: { ...base, phase: 'working', elapsed: '42:10', staleSince: '9:31' },
    botSession: { phase: 'working', agent, elapsed: '42:10' },
  } satisfies Record<string, TurnActivity>;
}
export type StateName = keyof ReturnType<typeof useActivities>;

export function Card({ state }: { state: StateName }) {
  return <LiveActivityCard activity={useActivities()[state]} />;
}
export function Compact({ state }: { state: StateName }) {
  return <IslandCompact activity={useActivities()[state]} />;
}
export function Minimal({ state }: { state: StateName }) {
  return <IslandMinimal activity={useActivities()[state]} />;
}
export function Expanded({ state }: { state: StateName }) {
  return <IslandExpanded activity={useActivities()[state]} />;
}

