import { MemoryRouter } from 'react-router-dom';
import { SettingsModelsPage } from '../../src/components/settings/models/SettingsModelsPage';
import { ToastProvider } from '../../src/context/ToastProvider';
import { installShellFixture } from './shellApi';

if (new URLSearchParams(location.search).get('view') === 'shell') installShellFixture();

export function ShellFixture() {
  return <MemoryRouter><ToastProvider>
    <main className="mx-auto min-w-0 max-w-[1100px] p-4"><SettingsModelsPage /></main>
  </ToastProvider></MemoryRouter>;
}
