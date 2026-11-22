// The shipped settings component and Desktop adapter, with IPC carried over
// stdio to the compiled AppKit peer rather than a JavaScript authentication fake.
import { createRoot } from 'react-dom/client';
import '../../src/i18n';
import '../../src/index.css';
import { AppLockSetting } from '../../src/components/settings/AppLockSetting';

createRoot(document.getElementById('root')!).render(<main className="p-8"><AppLockSetting /></main>);
