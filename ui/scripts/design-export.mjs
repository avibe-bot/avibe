#!/usr/bin/env node
// Export design frames for a Show Page: the live canvas (real components, interactive),
// a screenshot of each frame, frames.json, and the <DesignFrame> component that embeds them.
//
//   npm run design:export -- sidebar/bots                    into this Agent Session's Show Page
//   npm run design:export -- sidebar/bots --show <workspace> into another Show Page workspace
//
// Then, in a page of that Show workspace:
//   import { DesignFrame } from "../design/DesignFrame"
//   <DesignFrame id="sidebar/bots/three" />
import { execFileSync } from 'node:child_process';
import { copyFile, mkdir } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const uiRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const index = args.indexOf('--show');
const show = resolve(index === -1 ? JSON.parse(execFileSync('vibe', ['show', 'path'], { encoding: 'utf8' })).path : args.splice(index, 2)[1]);
const out = join(show, 'public', 'design');
const node = (script, extra) => execFileSync(process.execPath, [join(uiRoot, 'scripts', script), ...extra], { stdio: ['ignore', 'ignore', 'inherit'] });

node('design-build.mjs', ['--out', join(out, 'canvas')]);
let failed = false;
try {
  node('design-shot.mjs', ['--out', join(out, 'shots'), '--manifest', join(out, 'frames.json'), ...args]);
} catch {
  failed = true;
}
await mkdir(join(show, 'src', 'design'), { recursive: true });
await copyFile(join(uiRoot, 'design', 'export', 'DesignFrame.tsx'), join(show, 'src', 'design', 'DesignFrame.tsx'));
console.log(`Exported to ${out}\nIn a page of ${show}:\n  import { DesignFrame } from "../design/DesignFrame"\n  <DesignFrame id="<frame id>" />`);
process.exit(failed ? 1 : 0);
