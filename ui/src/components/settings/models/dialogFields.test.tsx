// A label that does not name its control, asserted the only way that means
// anything: from the rendered markup.
//
// The defect this file exists for was invisible in review — a visible 「New API
// key」 label sat directly above the password input, and assistive tech still
// reached an unnamed field, because nothing connected the two. `Field` now mints
// the id, so the check is that the id actually ARRIVES: `Input`/`Select` both
// spread onto their native element, and a primitive that stopped doing so would
// take the association down with it while looking identical on screen.
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { KeyRound } from 'lucide-react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Field } from './dialogFields';

/** The label's target and the control's own id, as the browser would read them. */
const pair = (markup: string) => ({
  labelFor: /<label[^>]*\bfor="([^"]*)"/.exec(markup)?.[1],
  controlId: /<(?:input|select)[^>]*\bid="([^"]*)"/.exec(markup)?.[1],
});

describe('Field', () => {
  it('names an icon-inset input', () => {
    const { labelFor, controlId } = pair(
      renderToStaticMarkup(
        <Field label="New API key" mono icon={KeyRound}>
          {(id) => <Input id={id} type="password" />}
        </Field>,
      ),
    );
    expect(labelFor).toBeTruthy();
    expect(controlId).toBe(labelFor);
  });

  it('names a control with no icon inset', () => {
    // A field with no icon takes the same path minus the wrapper — the branch
    // that renders `children(id)` bare is the one that could drop the argument.
    const { labelFor, controlId } = pair(
      renderToStaticMarkup(
        <Field label="Vendor">
          {(id) => (
            <Select id={id}>
              <option value="anthropic">Anthropic</option>
            </Select>
          )}
        </Field>,
      ),
    );
    expect(labelFor).toBeTruthy();
    expect(controlId).toBe(labelFor);
  });

  it('gives each field its own id', () => {
    // Two fields in one dialog: a shared constant id would associate the second
    // label with the first control, which reads as correct in the markup diff.
    const markup = renderToStaticMarkup(
      <>
        <Field label="One">{(id) => <Input id={id} />}</Field>
        <Field label="Two">{(id) => <Input id={id} />}</Field>
      </>,
    );
    const ids = [...markup.matchAll(/<input[^>]*\bid="([^"]*)"/g)].map((m) => m[1]);
    expect(ids).toHaveLength(2);
    expect(new Set(ids).size).toBe(2);
  });

  it('renders a hint only when there is one', () => {
    const withHint = renderToStaticMarkup(
      <Field label="Base URL" hint="Leave empty for the default">
        {(id) => <Input id={id} />}
      </Field>,
    );
    expect(withHint).toContain('Leave empty for the default');
    expect(renderToStaticMarkup(<Field label="Base URL">{(id) => <Input id={id} />}</Field>)).not.toContain('<p');
  });
});

describe('the class: the models dialogs do not hand-roll a field', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const files = readdirSync(here, { recursive: true, encoding: 'utf8' }).filter(
    (f) => /\.tsx$/.test(f) && !/\.test\.tsx$/.test(f),
  );
  const read = (f: string) => readFileSync(join(here, f), 'utf8');

  it('sweeps a non-trivial file set', () => {
    expect(files.length).toBeGreaterThan(10);
    expect(files).toContain('AddApiKeyDialog.tsx');
  });

  /**
   * An upper bound, not an equality: a NEW file that writes its own `<label>` is
   * the regression. The shared field itself is the only allowed owner.
   */
  const MAY_WRITE_A_LABEL = ['dialogFields.tsx'];

  it('leaves <label> to the shared field', () => {
    const handRolled = files.filter((f) => /<label\b/.test(read(f)));
    expect(handRolled.filter((f) => !MAY_WRITE_A_LABEL.includes(f))).toEqual([]);
  });

  it('has no user-visible placeholder outside the locale bundles', () => {
    // `placeholder="sk-…"` was display text a translator could never reach. The
    // literal form is the tell; `placeholder={t(...)}` is the shape that stays.
    expect(files.filter((f) => /placeholder="/.test(read(f)))).toEqual([]);
  });
});
