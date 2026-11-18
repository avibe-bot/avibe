import { HealthRequestCardView } from '@/components/health/HealthRequest';
import type { HealthViewState } from '@/lib/healthRequests';
import { frames } from '../../frame';
import { workbenchFixtures } from '../workbench/fixtures';

const states: HealthViewState[] = ['authorize', 'waiting', 'claimed', 'connected', 'succeeded', 'declined', 'failed', 'canceled', 'expired', 'needsUpdate'];
const designFrames = frames([
  { name: 'phone', title: 'Health request · iPhone authorization', width: 402, height: 360, status: 'shipped', fixtures: workbenchFixtures(), render: () => <div className="p-6"><HealthRequestCardView state="authorize" /></div> },
  { name: 'desktop', title: 'Health request · waiting for iPhone', width: 808, height: 240, status: 'shipped', fixtures: workbenchFixtures(), render: () => <div className="p-6"><HealthRequestCardView state="waiting" /></div> },
  { name: 'states', title: 'Health request · server states and connection receipt', width: 808, height: 1250, status: 'shipped', fixtures: workbenchFixtures(), render: () => <div className="flex flex-col gap-2 p-6">{states.map(state => <HealthRequestCardView key={state} state={state} />)}<HealthRequestCardView state="waiting" operation="health.refresh" /></div> },
]);

export default designFrames;
