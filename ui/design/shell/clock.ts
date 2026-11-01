// Frames show relative times ("8 minutes ago"), so the design clock is fixed:
// a screenshot taken today and one taken next month are the same picture.
export const DESIGN_NOW = Date.UTC(2026, 10, 1, 8, 0);

export function installFixedClock() {
  const RealDate = Date;
  class FixedDate extends RealDate {
    constructor(...args: ConstructorParameters<typeof Date> | []) {
      if (args.length === 0) super(DESIGN_NOW);
      else super(...(args as ConstructorParameters<typeof Date>));
    }
    static now() {
      return DESIGN_NOW;
    }
  }
  globalThis.Date = FixedDate as DateConstructor;
}
