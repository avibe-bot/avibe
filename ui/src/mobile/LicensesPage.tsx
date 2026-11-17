import { ChevronRight, ExternalLink, type LucideIcon, ScrollText, ShieldCheck } from 'lucide-react';
import type { MouseEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { type AppLicenseComponent, type AppLicenses, licenseBlocks } from './appLicenses';

/** The mobile App's raised card (`raised` in MobileOnboarding). */
const raised = 'rounded-3xl border border-edge bg-surface/80 p-5 shadow-elevation-0';
const textLink =
  'inline-flex min-h-11 items-center gap-1 text-sm font-medium text-foreground underline underline-offset-4';

type AboutRowProps = { icon: LucideIcon; label: string; disabled?: boolean; onClick?: () => void };

function AboutRow({ icon: Icon, label, disabled, onClick }: AboutRowProps) {
  return (
    <button
      type="button"
      disabled={disabled}
      className={cn(
        raised,
        'flex w-full items-center gap-3 text-left transition-transform duration-(--motion-duration-instant) active:scale-(--motion-scale-press) disabled:opacity-60',
      )}
      onClick={onClick}
    >
      <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-mint-soft">
        <Icon className="size-5 text-mint-ink" aria-hidden="true" />
      </span>
      <span className="min-w-0 flex-1 text-base font-semibold">{label}</span>
      <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden="true" />
    </button>
  );
}

/**
 * Account's About section: the App's own information, not the instance's. The privacy policy is
 * always there; the licenses once their notices have loaded. Design: mobile-app/licenses/entry.
 */
export function AboutSection({
  disabled,
  licenses,
  onLicenses,
  onPrivacy,
}: {
  disabled?: boolean;
  licenses: boolean;
  onLicenses?: () => void;
  onPrivacy?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <section className="mt-8">
      <h2 className="mb-4 text-sm font-semibold">{t('mobileApp.licenses.about')}</h2>
      <div className="flex flex-col gap-3">
        {onPrivacy ? (
          <AboutRow icon={ShieldCheck} label={t('mobileApp.account.privacy')} disabled={disabled} onClick={onPrivacy} />
        ) : null}
        {licenses ? (
          <AboutRow icon={ScrollText} label={t('mobileApp.licenses.title')} disabled={disabled} onClick={onLicenses} />
        ) : null}
      </div>
    </section>
  );
}

/**
 * The last thing on Account: deleting the account on avibe.bot, which the App shows over this
 * page with the App's sign-in.
 */
export function DeleteAccount({ disabled, onDelete }: { disabled?: boolean; onDelete: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="mt-auto pt-9 pb-3">
      <Button
        type="button"
        variant="link"
        disabled={disabled}
        className="px-0 text-destructive-ink"
        onClick={onDelete}
      >
        {t('mobileApp.account.delete')}
        <ExternalLink className="size-4" aria-hidden="true" />
      </Button>
      <p className="mt-2 text-xs leading-6 text-muted">{t('mobileApp.account.deleteHint')}</p>
    </div>
  );
}

/**
 * The Open-source licenses page below its title: the MPL-2.0 source offer
 * first, then each distinct license text once with the components it covers.
 * Design: mobile-app/licenses/default and group-open.
 */
export function LicensesContent({
  licenses,
  onOpenLink,
  openGroup,
}: {
  licenses: AppLicenses;
  onOpenLink?: (url: string) => void;
  /** A group drawn open, for design frames; people open groups themselves. */
  openGroup?: number;
}) {
  const { t } = useTranslation();
  const separator = t('mobileApp.licenses.separator');
  const crateName = (name: string, version: string) => t('mobileApp.licenses.crate', { name, version });
  const component = ({ name, version, authors }: AppLicenseComponent) =>
    authors?.length
      ? t('mobileApp.licenses.withAuthors', { crate: crateName(name, version), authors: authors.join(separator) })
      : crateName(name, version);
  const open = (url: string) => (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    onOpenLink?.(url);
  };
  return (
    <>
      {licenses.sources.length ? (
        <section className={raised} aria-labelledby="licenses-source">
          <h2 id="licenses-source" className="text-base font-semibold">
            {t('mobileApp.licenses.sourceTitle')}
          </h2>
          <p className="mt-2 text-sm leading-6 text-muted">{t('mobileApp.licenses.sourceBody')}</p>
          <ul className="mt-4 divide-y divide-edge border-t border-edge">
            {licenses.sources.map((source) => {
              const [first, ...related] = source.crates;
              const label = related.length
                ? t('mobileApp.licenses.family', { name: first.name, related: related.length, version: source.version })
                : crateName(first.name, source.version);
              return (
                <li key={source.upstream + source.version} className="py-4 last:pb-0">
                  <p className="break-words text-sm font-semibold">
                    {related.length ? (
                      label
                    ) : (
                      <>
                        {first.name} <span className="font-normal text-muted">{source.version}</span>
                      </>
                    )}
                  </p>
                  {related.length ? (
                    <p className="mt-1 break-words text-xs leading-6 text-muted">
                      {t('mobileApp.licenses.archives')}
                      {source.crates.map((crate, index) => (
                        <span key={crate.name}>
                          {index > 0 ? separator : null}
                          <a
                            href={crate.registry}
                            className="underline underline-offset-2"
                            aria-label={t('mobileApp.licenses.archiveFor', { name: crateName(crate.name, source.version) })}
                            onClick={open(crate.registry)}
                          >
                            {crate.name}
                          </a>
                        </span>
                      ))}
                    </p>
                  ) : null}
                  <p className="mt-2 flex flex-wrap items-center gap-x-5">
                    {related.length ? null : (
                      <a
                        href={first.registry}
                        className={textLink}
                        aria-label={t('mobileApp.licenses.archiveFor', { name: label })}
                        onClick={open(first.registry)}
                      >
                        {t('mobileApp.licenses.archive')}
                        <ExternalLink className="size-3.5" aria-hidden="true" />
                      </a>
                    )}
                    <a
                      href={source.upstream}
                      className={textLink}
                      aria-label={t('mobileApp.licenses.sourceFor', { name: label })}
                      onClick={open(source.upstream)}
                    >
                      {t('mobileApp.licenses.sourceCode')}
                      <ExternalLink className="size-3.5" aria-hidden="true" />
                    </a>
                  </p>
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}
      <section className="mt-8" aria-labelledby="licenses-list">
        <h2 id="licenses-list" className="mb-4 text-sm font-semibold">
          {t('mobileApp.licenses.texts')}
        </h2>
        <div className="divide-y divide-edge rounded-3xl border border-edge bg-surface/80 shadow-elevation-0">
          {licenses.groups.map((group, index) => (
            <details key={index} className="group" open={openGroup === index || undefined}>
              <summary className="flex min-h-11 cursor-pointer list-none items-center gap-3 p-5 [&::-webkit-details-marker]:hidden">
                <span className="min-w-0 flex-1">
                  <span className="block break-words text-base font-semibold">{group.title}</span>
                  <span className="mt-1 block text-xs text-muted">
                    {t('mobileApp.licenses.components', { count: group.components.length })}
                  </span>
                </span>
                <ChevronRight
                  className="size-4 shrink-0 text-muted transition-transform group-open:rotate-90"
                  aria-hidden="true"
                />
              </summary>
              <div className="px-5 pb-5">
                <p className="text-xs font-semibold">{t('mobileApp.licenses.covers')}</p>
                <p className="mt-1 break-words text-xs leading-5 text-muted">
                  {group.components.map(component).join(separator)}
                </p>
                <div className="mt-4 border-t border-edge pt-4">
                  {licenseBlocks(group.text).map((block, blockIndex) =>
                    block.kind === 'heading' ? (
                      <h3 key={blockIndex} className="mt-4 break-words text-xs font-semibold leading-5 first:mt-0">
                        {block.text}
                      </h3>
                    ) : (
                      <p
                        key={blockIndex}
                        className={cn(
                          'mt-3 break-words text-xs leading-5 text-muted first:mt-0',
                          block.kind === 'lines' && 'whitespace-pre-wrap',
                        )}
                      >
                        {block.text}
                      </p>
                    ),
                  )}
                </div>
              </div>
            </details>
          ))}
        </div>
      </section>
      <p className="pt-7 pb-3 text-center text-xs leading-6 text-muted">{t('mobileApp.licenses.generated')}</p>
    </>
  );
}
