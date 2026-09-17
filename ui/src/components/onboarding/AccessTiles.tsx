import { useEffect, useState } from 'react';
import { useReducedMotion } from 'framer-motion';
import { useTranslation } from 'react-i18next';
import { PlatformIcon } from '../visual';

const PLATFORMS = ['avibe', 'slack', 'discord', 'telegram', 'lark', 'wechat'];

export function AccessTiles({ paused }: { paused: boolean }) {
  const { t } = useTranslation();
  const reducedMotion = useReducedMotion() === true;
  const [pointerInside, setPointerInside] = useState(false);
  const [focusInside, setFocusInside] = useState(false);
  const [emphasis, setEmphasis] = useState<number | null>(null);
  const automatic = !paused && !reducedMotion && !pointerInside && !focusInside;
  useEffect(() => {
    if (!automatic) return;
    const timer = window.setInterval(() => {
      setEmphasis((previous) => previous === null ? Math.floor(Math.random() * PLATFORMS.length)
        : (previous + 1 + Math.floor(Math.random() * (PLATFORMS.length - 1))) % PLATFORMS.length);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [automatic]);

  return (
    <section className="onboarding-access" aria-label={t('onboarding.access.label')}>
      <p>{t('onboarding.access.description')}</p>
      <ul className="onboarding-access-grid" onPointerEnter={() => setPointerInside(true)} onPointerLeave={() => setPointerInside(false)}
        onFocus={() => setFocusInside(true)} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setFocusInside(false); }}>
        {PLATFORMS.map((platform, index) => (
          <li key={platform} tabIndex={0} className="onboarding-access-tile" data-emphasis={automatic && emphasis === index}>
            <span className="onboarding-access-icon"><PlatformIcon platform={platform} size={22} aria-hidden="true" /></span>
            <span>{t(`onboarding.access.${platform}`)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
