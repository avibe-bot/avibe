import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronUp } from 'lucide-react';

/** The up and down arrows beside an order position: one stacked control, quiet until hovered. */
export const OrderStepper: React.FC<{
  label: string;
  canUp: boolean;
  canDown: boolean;
  pending: boolean;
  onMove: (direction: -1 | 1) => void;
  buttonRef?: (direction: -1 | 1, node: HTMLButtonElement | null) => void;
}> = ({ label, canUp, canDown, pending, onMove, buttonRef }) => {
  const { t } = useTranslation();
  return (
    <span className="model-hub-order-stepper">
      {([-1, 1] as const).map((direction) => (
        <button
          key={direction}
          ref={buttonRef ? (node) => buttonRef(direction, node) : undefined}
          type="button"
          data-direction={direction}
          className="model-hub-order-step"
          aria-label={t(direction === -1 ? 'settings.models.tools.order.moveUp' : 'settings.models.tools.order.moveDown', { source: label }) as string}
          // While a write is out the arrows stay focusable and do nothing:
          // the next move must be built on the order that write answers.
          aria-disabled={pending || undefined}
          disabled={direction === -1 ? !canUp : !canDown}
          onClick={() => onMove(direction)}
        >
          {direction === -1 ? <ChevronUp aria-hidden /> : <ChevronDown aria-hidden />}
        </button>
      ))}
    </span>
  );
};
