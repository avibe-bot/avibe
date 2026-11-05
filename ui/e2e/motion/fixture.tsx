// The shared surfaces that move (docs/plans/web-ui-motion.md M6, M9), as the product renders them: a
// Dialog (a sheet on a phone), a ResponsiveMenu, a Popover, and toasts. No backend; nothing persists.
import { StrictMode, useState } from 'react';
import { createRoot } from 'react-dom/client';

import { Button } from '../../src/components/ui/button';
import { Dialog, DialogContent, DialogTitle } from '../../src/components/ui/dialog';
import { Popover, PopoverContent, PopoverTrigger } from '../../src/components/ui/popover';
import { ResponsiveMenu } from '../../src/components/ui/responsive-menu';
import { useToast } from '../../src/context/ToastContext';
import { ToastProvider } from '../../src/context/ToastProvider';
import '../../src/i18n';
import '../../src/index.css';

export function Surfaces() {
  const { showToast } = useToast();
  const [dialog, setDialog] = useState(false);
  const [menu, setMenu] = useState(false);
  const [toasts, setToasts] = useState(0);
  return (
    <div className="flex flex-col items-start gap-3 p-4">
      <Button onClick={() => setDialog(true)}>Open dialog</Button>
      <Dialog open={dialog} onOpenChange={setDialog}>
        <DialogContent aria-describedby={undefined} closeLabel="Close">
          <DialogTitle>Every Bot</DialogTitle>
          <ul data-testid="dialog-list" className="flex flex-col gap-1">
            {Array.from({ length: 40 }, (_, i) => <li key={i} className="rounded-lg border border-border px-3 py-3">Bot {i + 1}</li>)}
          </ul>
        </DialogContent>
      </Dialog>
      <ResponsiveMenu open={menu} onOpenChange={setMenu} trigger={<Button variant="outline">Open menu</Button>} sheetTitle="Session">
        <button type="button" className="px-4 py-3 text-left">Rename</button>
        <button type="button" className="px-4 py-3 text-left">Archive</button>
      </ResponsiveMenu>
      <Popover>
        <PopoverTrigger asChild><Button variant="outline">Open popover</Button></PopoverTrigger>
        <PopoverContent>Popover body</PopoverContent>
      </Popover>
      <Button
        variant="secondary"
        onClick={() => {
          setToasts((n) => n + 1);
          showToast(`Saved ${toasts + 1}`);
        }}
      >
        Toast
      </Button>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ToastProvider>
      <div className="min-h-dvh bg-background text-foreground">
        <Surfaces />
      </div>
    </ToastProvider>
  </StrictMode>,
);
