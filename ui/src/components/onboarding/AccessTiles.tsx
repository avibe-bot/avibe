import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { PlatformIcon } from '../visual';
import { useOnboardingMotion } from './motion';

const PLATFORMS = ['avibe', 'slack', 'discord', 'telegram', 'lark', 'wechat'];

export function AccessTiles() {
  const { t } = useTranslation();
  const { running } = useOnboardingMotion();
  const [pointerInside, setPointerInside] = useState(false);
  const [focusInside, setFocusInside] = useState(false);
  const [emphasis, setEmphasis] = useState<number | null>(null);
  // Same running state as the story, so a hidden tab or a reduced-motion preference
  // stops both; pointer and keyboard interaction still yield the emphasis to the user.
  const automatic = running && !pointerInside && !focusInside;
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
