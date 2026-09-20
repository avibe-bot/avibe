type LocalCalendarDate = Readonly<{ year: number; month: number; day: number }>;

const dateParts = (date: Date, timeZone?: string): LocalCalendarDate => {
  if (timeZone === undefined) {
    return { year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate() };
  }
  const values = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone,
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
    }).formatToParts(date).map(({ type, value }) => [type, value]),
  );
  return { year: Number(values.year), month: Number(values.month), day: Number(values.day) };
};

const previousDate = ({ year, month, day }: LocalCalendarDate): LocalCalendarDate => {
  const previous = new Date(Date.UTC(year, month - 1, day - 1));
  return {
    year: previous.getUTCFullYear(),
    month: previous.getUTCMonth() + 1,
    day: previous.getUTCDate(),
  };
};

const sameDate = (left: LocalCalendarDate, right: LocalCalendarDate): boolean =>
  left.year === right.year && left.month === right.month && left.day === right.day;

export const localCalendarRelation = (
  date: Date,
  now: Date,
  timeZone?: string,
): 'today' | 'yesterday' | 'other' => {
  const current = dateParts(now, timeZone);
  const candidate = dateParts(date, timeZone);
  if (sameDate(candidate, current)) return 'today';
  return sameDate(candidate, previousDate(current)) ? 'yesterday' : 'other';
};
