import React from 'react';
import { ArrowRight, HardDrive, PlugZap, Radio, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { WizardCard } from '../visual';
import { Button } from '../ui/button';

interface WelcomeProps {
  onNext: (data: any) => void;
}

// Mirrors design.pen welCard (EYBEx): 920px card, 80×80 mint logo with strong glow,
// 42px title, 16px subtitle (max 680), three accented feature tiles, large mint
// "Get started" button. Card colors: #11111c with 1px white/8 stroke and a soft
// mint shadow (#5BFFA014, blur 64, y32, spread -12).
export const Welcome: React.FC<WelcomeProps> = ({ onNext }) => {
  const { t } = useTranslation();

  const features = [
    {
      Icon: HardDrive,
      iconClass: 'text-mint-ink',
      title: t('welcome.card1Title'),
      body: t('welcome.feature1'),
    },
    {
      Icon: PlugZap,
      iconClass: 'text-cyan-ink',
      title: t('welcome.card2Title'),
      body: t('welcome.feature2'),
    },
    {
      Icon: Sparkles,
      iconClass: 'text-violet-ink',
      title: t('welcome.card3Title'),
      body: t('welcome.feature3'),
    },
  ];

  return (
    <div className="flex w-full justify-center">
      <WizardCard
        size="hero"
        className="items-center text-center gap-8"
      >
        {/* welBigLogo (A3aTA) */}
        <div
          className="mx-auto flex size-20 items-center justify-center rounded-[20px] border-2 border-mint/40 bg-mint/[0.16] shadow-glow-lg-mint"
          aria-hidden
        >
          <Radio className="size-10 text-mint-ink" strokeWidth={1.75} />
        </div>

        {/* welHead (OpTFX) gap 14 */}
        <div className="flex flex-col items-center gap-3.5">
          <h1 className="text-[28px] font-bold leading-[1.1] tracking-[-0.4px] text-foreground sm:text-[42px] sm:leading-[1.05] sm:tracking-[-0.8px]">
            {t('welcome.title')}
          </h1>
          <p className="max-w-[680px] text-[16px] leading-[1.55] text-muted">
            {t('welcome.subtitle')}
          </p>
        </div>

        {/* welHilights (QdiUZ) — three feature tiles, gap 16 */}
        <div className="grid w-full gap-4 text-left md:grid-cols-3">
          {features.map(({ Icon, iconClass, title, body }) => (
            <div
              key={title}
              className="flex flex-col gap-2 rounded-xl border border-foreground/[0.08] bg-background px-4 py-[18px]"
            >
              <Icon className={`size-5 ${iconClass}`} strokeWidth={1.75} />
              <div className="text-[14px] font-semibold leading-tight text-foreground">{title}</div>
              <p className="text-[12px] leading-[1.5] text-muted">{body}</p>
            </div>
          ))}
        </div>

        {/* welStart (a95QY) — large mint pill */}
        <Button type="button" variant="brand" size="hero" onClick={() => onNext({})} className="group">
          {t('welcome.getStarted')}
          <ArrowRight className="size-4 transition-transform group-hover:translate-x-0.5" strokeWidth={2.25} />
        </Button>
      </WizardCard>
    </div>
  );
};
