// The phone shell's demo data: projects with their Sessions, as the shared project tree serves them.
import type { WorkbenchSession } from '@/context/ApiContext';

import { demoProjects, demoSessions, session } from '../../shell/fixtures';

export const phoneSessions: Record<string, WorkbenchSession[]> = {
  ...demoSessions,
  proj_relay: [session('ses_relay_1', 'proj_relay', '中转节点限流', { agent_status: 'idle' })],
  proj_work: [
    session('ses_work_1', 'proj_work', '周报整理'),
    session('ses_work_2', 'proj_work', '面试题库'),
    session('ses_work_3', 'proj_work', '季度 OKR 草稿'),
  ],
  proj_article: [session('ses_article_1', 'proj_article', '长文：本地优先的 Agent OS')],
  proj_askill: [session('ses_askill_1', 'proj_askill', 'skill 打包规范'), session('ses_askill_2', 'proj_askill', '发布 0.1.14')],
  proj_chainbot: [
    session('ses_audit', 'proj_chainbot', '合约审计', { agent_name: 'reviewer', agent_backend: 'claude' }),
    session('ses_gas', 'proj_chainbot', 'Gas 优化方案', { agent_status: 'running', agent_name: 'codex', agent_backend: 'codex' }),
  ],
};

export const phoneProjects = demoProjects;
