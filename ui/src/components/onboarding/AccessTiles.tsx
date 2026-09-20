import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { PlatformIcon } from '../visual';
import { useOnboardingMotion } from './motion';

const PLATFORMS = ['avibe', 'slack', 'discord', 'telegram', 'lark', 'wechat'] as const;

export function AccessTiles() {
  const { t } = useTranslation();
  const { ref, running } = useOnboardingMotion();
  const [pointerInside, setPointerInside] = useState(false);
  const [focusInside, setFocusInside] = useState(false);
  const [emphasis, setEmphasis] = useState<number | null>(null);
  // Same lifecycle as the story: a hidden tab, a reduced-motion preference, or these
  // tiles being scrolled out of sight stops the rotation; pointer and keyboard
  // interaction still yield the emphasis to the user.
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
    <section ref={ref} className="onboarding-access" aria-label={t('onboarding.access.label')}>
      <p>{t('onboarding.access.description')}</p>
      <ul className="onboarding-access-grid" onPointerEnter={() => setPointerInside(true)} onPointerLeave={() => setPointerInside(false)}
        onFocus={() => setFocusInside(true)} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setFocusInside(false); }}>
        {PLATFORMS.map((platform, index) => (
          // `data-connects` is what the mint emphasis is keyed on. The five chat
          // platforms are the ones Avibe connects *to*; the mobile app is Avibe
          // itself, so it lifts on hover like the rest but is never lit up as a
          // connection the user still has to make.
          <li key={platform} tabIndex={0} className="onboarding-access-tile"
            data-connects={platform !== 'avibe'} data-emphasis={automatic && emphasis === index}>
            <span className="onboarding-access-icon"><PlatformIcon platform={platform} size={26} aria-hidden="true" /></span>
            <span className="onboarding-access-label">{t(`onboarding.access.${platform}`)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
