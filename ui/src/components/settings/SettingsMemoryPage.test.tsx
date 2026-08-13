/* @vitest-environment jsdom */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import { SettingsMemoryPage } from './SettingsMemoryPage';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../../context/ApiContext', () => ({
  useApi: () => ({
    memorySettings: vi.fn().mockResolvedValue(null),
  }),
}));

describe('SettingsMemoryPage', () => {
  it('mounts the memory settings surface', () => {
    render(<SettingsMemoryPage />);
    expect(screen.getByText(/memory/i)).toBeTruthy();
  });
});
