// Typed display fixtures for hermetic Model Hub UI tests.
import mockCorpusJson from './modelHubMockCorpus.json';
import type {
  ResolutionEvent,
  RuntimeDependency,
  Source,
} from './types';

const corpus = mockCorpusJson as unknown as {
  seed: { reads: { sources: Source[] } };
};

/** The server-generated seed projection; this helper never derives Source policy. */
export function buildMockSources(): Source[] {
  return structuredClone(corpus.seed.reads.sources);
}

const iso = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString();
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
export function buildMockEvents(): ResolutionEvent[] {
  // Stored in display order (adapter-owned feed order); the UI renders as-is.
  return [
    {
      id: 'evt_a',
      ts: iso(-2 * HOUR - 11 * MIN),
      agent: 'claude',
      kind: 'switch',
      model_id: 'claude-opus-4-6',
      from_source: 'src_claudepro1',
      to_source: 'src_anthkey01',
      reason: 'quota_exhausted',
      billing_note: 'entered_metered',
      severity: 'info',
      human_zh: 'Claude Code：Claude Pro 本周期额度用完 → 已切到 Anthropic API Key（按量）',
      human_en: 'Claude Code: Claude Pro cycle quota exhausted → switched to Anthropic API Key (metered)',
    },
    {
      id: 'evt_b',
      ts: iso(-38 * MIN),
      agent: 'claude',
      kind: 'recover',
      model_id: 'claude-opus-4-6',
      from_source: 'src_anthkey01',
      to_source: 'src_claudepro1',
      reason: 'recovery',
      billing_note: 'left_metered',
      severity: 'info',
      human_zh: 'Claude Code：Claude Pro 额度恢复 → 已切回订阅',
      human_en: 'Claude Code: Claude Pro quota recovered → switched back to the subscription',
    },
    {
      id: 'evt_c',
      ts: iso(-1 * DAY - 30 * MIN),
      agent: 'system',
      kind: 'cooldown',
      model_id: 'glm-5.2-air',
      from_source: 'src_relay9c1x',
      to_source: null,
      reason: 'network',
      billing_note: null,
      severity: 'info',
      human_zh: 'relay.example 连续超时 → 暂停使用 1 小时，期间自动跳过',
      human_en: 'relay.example timed out repeatedly → paused for 1 hour, skipped automatically',
    },
    // AC-18's render-time 「已删除」 case: a retained event whose endpoint no
    // longer resolves. The recorded sentence still names the source, because it
    // was composed when the source existed — that string IS the snapshot.
    {
      id: 'evt_d',
      ts: iso(-2 * DAY - 4 * HOUR),
      agent: 'codex',
      kind: 'needs_action',
      model_id: 'gpt-5.6',
      from_source: 'src_oldkey0099',
      to_source: null,
      reason: 'credential_revoked',
      billing_note: null,
      severity: 'action_required',
      human_zh: 'Codex：旧 OpenAI API Key 凭证已失效 → 已跳过，需要重新授权',
      human_en: 'Codex: the old OpenAI API Key credential was revoked → skipped, re-authorization needed',
    },
    // A history tail, so 「查看全部」 crosses a page boundary in the mock. With
    // four rows the cursor path was unreachable in dev — the very case the feed's
    // pagination exists for could only be seen against a real server.
    // Deep enough that the feed needs THREE pages at the page size 最近切换 asks
    // for (20): expanding 查看全部 pulls the second page on its own, so anything
    // shallower exhausts the cursor before 查看更多 is ever clickable — which is
    // how the button shipped unexercised in the first place.
    ...olderHistory(45),
  ];
}

/** Filler older events, oldest last, ids stable so `before` cursors are stable. */
function olderHistory(count: number): ResolutionEvent[] {
  return Array.from({ length: count }, (_, i) => {
    const n = i + 1;
    const cooling = n % 3 === 0;
    return {
      id: `evt_h${String(n).padStart(2, '0')}`,
      ts: iso(-3 * DAY - i * (5 * HOUR + 20 * MIN)),
      agent: 'claude',
      kind: cooling ? 'cooldown' : 'switch',
      model_id: 'claude-opus-4-6',
      from_source: cooling ? 'src_relay9c1x' : 'src_claudepro1',
      to_source: cooling ? null : 'src_anthkey01',
      reason: cooling ? 'rate_limited' : 'quota_exhausted',
      billing_note: cooling ? null : 'entered_metered',
      severity: 'info',
      human_zh: cooling
        ? 'relay.example 触发限流 → 暂停使用 10 分钟，期间自动跳过'
        : 'Claude Code：Claude Pro 本周期额度用完 → 已切到 Anthropic API Key（按量）',
      human_en: cooling
        ? 'relay.example hit a rate limit → paused for 10 minutes, skipped automatically'
        : 'Claude Code: Claude Pro cycle quota exhausted → switched to Anthropic API Key (metered)',
    } satisfies ResolutionEvent;
  });
}

export function buildMockRuntime(): RuntimeDependency {
  return {
    contract_version: 5,
    manifest: {
      name: 'cliproxyapi',
      resolution: 'resolved',
      version: 'v7.2.95',
      source_sha: 'f71ec0eb6776854457892452cf28c47f0d658251',
      assets: [],
    },
    status: {
      installed_version: 'v7.2.95',
      verified: true,
      listening: { host: '127.0.0.1', port: 15220 },
      health: 'ok',
      last_check: iso(-3 * MIN),
    },
  };
}
