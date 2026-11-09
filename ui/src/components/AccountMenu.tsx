import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { LogOut, UserRound } from 'lucide-react';
import clsx from 'clsx';

import { accountAvatarClassName, accountInitialFor } from './AccountAvatar';
import { useAuthAccount } from '../lib/useAuthAccount';
import { hasNativeCapability, nativeShowAccount } from '../lib/native';

export const AccountMenu: React.FC<{ openUpward?: boolean }> = ({ openUpward = false }) => {
  const { t } = useTranslation();
  const { email, signingOut, signOut: handleSignOut } = useAuthAccount();
  const nativeAccount = hasNativeCapability('accountPresentation');
  const [isOpen, setIsOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setIsOpen(false);
      }
    };
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      document.addEventListener('keydown', handleKey);
    }
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKey);
    };
  }, [isOpen]);

  if (email === null) return null;

  const accountLabel = t('appShell.accountMenuLabel', { email });
  const handleAccountAction = async () => {
    setIsOpen(false);
    if (!nativeAccount || !await nativeShowAccount()) {
      await handleSignOut();
    }
  };

  return (
    <div className="relative" ref={wrapRef}>
      <button
        type="button"
        onClick={() => setIsOpen((v) => !v)}
        aria-label={accountLabel}
        title={accountLabel}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        className={clsx(
          accountAvatarClassName,
          'transition hover:bg-cyan/[0.16] hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring',
        )}
      >
        {accountInitialFor(email)}
      </button>

      {isOpen && (
        <div
          role="menu"
          className={clsx(
            'absolute z-50 min-w-[14rem] rounded-lg border border-edge bg-popover py-1 text-popover-foreground shadow-elevation-2',
            openUpward ? 'bottom-full left-0 mb-2' : 'top-full right-0 mt-2'
          )}
        >
          <div className="px-3 pt-2 text-[10px] font-bold uppercase tracking-[0.18em] text-muted">
            {t('appShell.signedInAs')}
          </div>
          <div className="px-3 pb-2 pt-0.5 text-[13px] font-medium text-foreground break-all">
            {email}
          </div>
          <div className="my-1 border-t border-border" />
          <button
            type="button"
            role="menuitem"
            onClick={() => void handleAccountAction()}
            disabled={signingOut}
            className={clsx(
              'flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-surface-2 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60',
              nativeAccount ? 'text-foreground' : 'text-muted',
            )}
          >
            {nativeAccount ? <UserRound className="size-4" /> : <LogOut className="size-4" />}
            <span>
              {nativeAccount
                ? t('appShell.openMobileAccount')
                : signingOut
                  ? t('appShell.signingOut')
                  : t('appShell.signOut')}
            </span>
          </button>
        </div>
      )}
    </div>
  );
};
