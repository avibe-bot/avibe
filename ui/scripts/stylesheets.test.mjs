import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import postcss from 'postcss';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { eachStylesheet } from './stylesheets.mjs';
import { colourRegistrationsIn, customPropertiesIn } from './customProperties.mjs';
import { declarationSpansIn } from './cssDeclarations.mjs';
import { withoutNonRenderingText } from './nonRenderingText.mjs';

const workspaces = [];
afterEach(() => {
  vi.restoreAllMocks();
  for (const dir of workspaces.splice(0)) fs.rmSync(dir, { recursive: true });
});

describe('CSS validation does not consume source maps', () => {
  it.each(['absolute', 'relative', 'inline'])('ignores %s sourceMappingURL in CSS and embedded styles', (kind) => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'avibe-css-map-'));
    workspaces.push(temp);
    const input = path.join(temp, 'styles');
    fs.mkdirSync(input);
    const previous = {
      version: 3, sources: ['synthetic.css'], names: [], mappings: 'AAAA',
      sourcesContent: ['harmless outside-tree source'],
    };
    const mapFile = path.join(temp, 'outside.map');
    fs.writeFileSync(mapFile, JSON.stringify(previous));
    const annotation = kind === 'absolute' ? mapFile.replaceAll('\\', '/')
      : kind === 'relative' ? '../outside.map'
        : `data:application/json;base64,${Buffer.from(JSON.stringify(previous)).toString('base64')}`;
    const css = `:root { --safe-token: blue; }\n/*# sourceMappingURL=${annotation} */`;
    fs.writeFileSync(path.join(input, 'plain.css'), css);
    fs.writeFileSync(path.join(input, 'embedded.tsx'), `const content = <style>{\`${css}\`}</style>;`);

    // A trusted explicit map is readable: the fixture is valid, not a missing
    // file that would make every implementation appear safe.
    const control = postcss.parse(css, { map: { prev: () => mapFile } });
    expect(control.source.input.map.consumer().sourcesContent).toEqual(previous.sourcesContent);

    const parsed = [...eachStylesheet(input)];
    expect(parsed).toHaveLength(2);
    for (const [, sheet] of parsed) {
      expect(sheet.source.input.map).toBeUndefined();
      expect(customPropertiesIn(sheet).get('--safe-token')).toEqual(new Set(['blue']));
    }
  });

  it('disables maps in every reusable text-parsing validation consumer', () => {
    const parse = vi.spyOn(postcss, 'parse');
    const css = ':root { --safe-token: blue; }';
    customPropertiesIn(css);
    colourRegistrationsIn(css);
    declarationSpansIn(css);
    withoutNonRenderingText(css, 'probe.css');
    expect(parse).toHaveBeenCalledTimes(4);
    for (const [, options] of parse.mock.calls) expect(options.map).toBe(false);
  });
});
