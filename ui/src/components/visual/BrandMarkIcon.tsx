import { BACKEND_BRAND_MARKS } from './backendBrandMarks';

type BrandMark = (typeof BACKEND_BRAND_MARKS)[keyof typeof BACKEND_BRAND_MARKS];

/** A brand mark drawn the way a lucide icon is, so a table of icons can hold either. */
export function brandMarkIcon(mark: BrandMark, displayName: string) {
  const Icon = ({ size = 24, className }: { size?: number; className?: string }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true" focusable="false">
      <path d={mark.path} fill={mark.fill} fillRule={mark.fillRule} />
    </svg>
  );
  Icon.displayName = displayName;
  return Icon;
}
