/* @vitest-environment jsdom */

import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { VaultSecretForm } from './vault-secret-form';

const api = vi.hoisted(() => ({
  listDependencies: vi.fn(async () => ({ ok: true, deps: [] })),
  listVaultSecrets: vi.fn(async () => ({ secrets: [] })),
  listSkills: vi.fn(async () => ({ skills: [] })),
  createVaultSecret: vi.fn(),
}));
vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
afterEach(cleanup);

it('checks only avault when the secret form opens, without creating a secret', async () => {
  render(<VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} />);
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalledWith({ ids: ['avault'] }));
  expect(api.listDependencies).toHaveBeenCalledTimes(1);
  expect(api.createVaultSecret).not.toHaveBeenCalled();
});
