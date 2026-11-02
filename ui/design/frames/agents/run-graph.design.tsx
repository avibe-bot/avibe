// Agents · Runs: the run graph of who started whom and who reports back, with Task and Watch triggers,
// and the panels a node or a trigger opens.
import { AgentGraphDetail } from '@/components/workbench/AgentGraphDetail';
import { AgentGraphTriggerDetail } from '@/components/workbench/AgentGraphTriggerDetail';
import { AgentsPage } from '@/components/workbench/AgentsPage';

import { frames } from '../../frame';
import { harnessApi } from '../harness/fixtures';
import { consolePage, phoneShell, workbenchFixtures } from '../workbench/fixtures';
import { agentsApi, graphApi, graphEdges, graphNodes, graphTriggers } from './fixtures';

const page = <AgentsPage />;
const fixtures = workbenchFixtures({ ...agentsApi(), ...harnessApi('tasks'), ...graphApi });
const nodesById = new Map(graphNodes.map((node) => [node.session_id, node]));
const triggersById = new Map(graphTriggers.map((trigger) => [trigger.definition_id, trigger]));

// The right-hand panel as the Runs tab frames it.
const panel = (children: React.ReactNode) => () => (
  <div className="h-full bg-background p-4">
    <div className="h-full overflow-y-auto rounded-2xl border border-border-strong bg-surface p-5">{children}</div>
  </div>
);

const designFrames = frames([
  {
    name: 'graph',
    title: 'Agents · 运行图（含历史）',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/agents?tab=running',
    fixtures,
    render: consolePage(page),
  },
  {
    name: 'node-detail',
    title: 'Agents · 运行图 · 会话节点详情',
    width: 400,
    height: 800,
    status: 'shipped',
    fixtures,
    render: panel(
      <AgentGraphDetail
        node={graphNodes[1]}
        nodesById={nodesById}
        edges={graphEdges}
        triggersById={triggersById}
        onClose={() => {}}
        onSelectNode={() => {}}
        onRefresh={() => {}}
      />,
    ),
  },
  {
    name: 'trigger-detail',
    title: 'Agents · 运行图 · 触发器详情',
    width: 400,
    height: 800,
    status: 'shipped',
    fixtures,
    render: panel(
      <AgentGraphTriggerDetail
        trigger={graphTriggers[0]}
        edges={graphEdges}
        nodesById={nodesById}
        onClose={() => {}}
        onSelectNode={() => {}}
        onRefresh={() => {}}
      />,
    ),
  },
  {
    name: 'node-selected',
    title: 'Agents · 运行图 · 选中一个会话节点',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/agents?tab=running',
    fixtures,
    click: ['text=avault 沙箱 · 后端 lane'],
    render: consolePage(page),
  },
  {
    name: 'phone',
    title: 'Agents · 运行图 · 手机（分组列表）',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/agents?tab=running',
    fixtures,
    render: phoneShell('/agents', page),
  },
]);

export default designFrames;
