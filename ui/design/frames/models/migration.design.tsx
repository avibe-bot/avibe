// 迁移配置: the shipped MigrationDialog, listing the native logins and keys it found on this
// machine that the gateway can take over.
import { MigrationDialog } from '@/components/settings/models/MigrationDialog';
import type { MigrationItem } from '@/components/settings/models/types';

import { frames } from '../../frame';
import { baseApi, ok } from './fixtures';

const items: MigrationItem[] = [
  {
    id: 'mig_claude_oauth', backend: 'claude', kind: 'oauth_native', masked_detail: 'Claude 本机登录 · lin@chainbot.io', proposed_action: 'keep_native',
    selected: true, notes_key: null, vendor: 'anthropic', display_name: 'Anthropic', masked_credential: null, source_paths: ['~/.claude/.credentials.json'],
  },
  {
    id: 'mig_codex_key', backend: 'codex', kind: 'api_key', masked_detail: 'sk-proj…7Fq2 + 自定义 Base URL', proposed_action: 'import',
    selected: true, notes_key: null, vendor: 'openai', display_name: 'OpenAI', masked_credential: 'sk-proj…7Fq2', source_paths: ['~/.codex/config.toml'], may_hold_api_key: true,
  },
  {
    id: 'mig_opencode_deepseek', backend: 'opencode', kind: 'opencode_provider', masked_detail: 'deepseek · sk-4f1…a09c', proposed_action: 'import',
    selected: true, notes_key: null, vendor: 'deepseek', display_name: 'deepseek', masked_credential: 'sk-4f1…a09c', source_paths: ['~/.config/opencode/opencode.json'], may_hold_api_key: true,
  },
];

const migration = () => <MigrationDialog open onClose={() => {}} />;

const designFrames = frames([
  {
    name: 'found',
    title: '迁移配置 · 发现本机配置',
    width: 1000,
    height: 820,
    status: 'shipped',
    fixtures: { api: { ...baseApi, '/api/models/migration/scan': ok({ scan: { items } }) } },
    render: migration,
  },
  {
    name: 'phone',
    title: '迁移配置 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: { api: { ...baseApi, '/api/models/migration/scan': ok({ scan: { items } }) } },
    render: migration,
  },
]);

export default designFrames;
