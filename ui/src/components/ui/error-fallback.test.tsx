// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { ErrorFallback } from './error-fallback';

beforeEach(async () => {
  await i18n.changeLanguage('en');
});

afterEach(cleanup);

function renderFallback(error: unknown, reset = vi.fn()) {
  render(
    <I18nextProvider i18n={i18n}>
      <ErrorFallback error={error} reset={reset} variant="page" />
    </I18nextProvider>,
  );
  return reset;
}

describe('ErrorFallback', () => {
  it.each([
    'Failed to fetch dynamically imported module: https://app.example/assets/SettingsModelsPage-D_wFDg8R.js',
    'Importing a module script failed.',
    'error loading dynamically imported module: https://app.example/assets/TerminalView-qpFWlPaW.js',
  ])('offers only a reload, with feedback, when a lazy module failed to load: %s', (message) => {
    renderFallback(new TypeError(message));

    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Reload page' }));

    const reloading = screen.getByRole('button', { name: 'Reloading…' });
    expect(reloading).toHaveProperty('disabled', true);
    expect(reloading.getAttribute('aria-busy')).toBe('true');
  });

  it('keeps retry for an ordinary render error', () => {
    const reset = renderFallback(new Error('Cannot read properties of undefined'));

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(reset).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: 'Reload page' })).toBeTruthy();
  });
});
