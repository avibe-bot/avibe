import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { MemoryStatus } from '../../../context/ApiContext';
import { MemoryStatusPanel } from './MemoryStatusPanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const STATUS: MemoryStatus = {
  status: 'ok',
  state: 'ready',
  buckets: { syncing: 2, succeeded: 17, unknown: 1, failed: 0, dead: 0, missed: 0 },
  pending: 2,
  processing: 0,
  awaiting_receipt: 0,
  succeeded: 17,
  receipt_unknown: 1,
  distill_failed: 0,
  dead: 0,
  missed: 0,
  queue_plaintext_bytes: 128,
  provider_disk_bytes: 2048,
  last_success_at: null,
  last_flush_observation: null,
  last_flush_status: null,
  last_flush_error_code: null,
  last_flush_request_id: null,
  last_flush_at: null,
  processing_fault_kind: null,
  processing_fault_since: null,
  processing_alert_active: false,
  error: null,
  data_exists: true,
};

const renderPanel = (status: MemoryStatus | null, error: string | null) =>
  renderToStaticMarkup(
    <MemoryStatusPanel
      status={status}
      failures={[]}
      failureRetentionDays={90}
      failuresError={null}
      loading={false}
      error={error}
      onRefresh={() => undefined}
      onOpenSettings={() => undefined}
      onRestartEngine={() => undefined}
      restarting={false}
    />,
  );

describe('MemoryStatusPanel', () => {
  it('keeps the last status visible behind a polling error banner', () => {
    const html = renderPanel(STATUS, 'poll failed');

    expect(html).toContain('poll failed');
    expect(html).toContain('memory.status.state.ready');
    expect(html).toContain('17');
  });

  it('renders only the error state before any status has loaded', () => {
    const html = renderPanel(null, 'initial load failed');

    expect(html).toContain('initial load failed');
    expect(html).not.toContain('memory.status.state.ready');
  });
});
