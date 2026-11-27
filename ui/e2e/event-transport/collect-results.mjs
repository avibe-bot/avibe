import assert from 'node:assert/strict';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const [directory, destination] = process.argv.slice(2);
if (!directory || !destination) throw new Error('Usage: node collect-results.mjs <evidence-directory> <summary.json>');
const results = readdirSync(directory).filter((name) => name.endsWith('.json'))
  .sort().map((name) => ({ file: name, ...JSON.parse(readFileSync(resolve(directory, name), 'utf8')) }));
assert.equal(results.length, 28, 'Expected the complete two-browser suite');
const runs = results.filter((result) => result.file.startsWith('compatibility-'));
const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
const summary = {
  at: new Date().toISOString(),
  cases: results.length,
  provenance: results[0].provenance,
  browsers: [...new Map(results.map((result) => [result.browser, result.browserVersion])).entries()]
    .map(([name, version]) => ({ name, version })),
  compatibility: runs.map((result) => ({
    browser: result.browser,
    family: result.family,
    client: result.client,
    runtime: result.runtime,
    documents: result.documentsLoadedBeforeStreams,
    transport: result.expectedTransport,
    streamAdmissions: { sse: result.statsBeforeCleanup.sseOpened, websocket: result.statsBeforeCleanup.wsOpened },
    fetchMilliseconds: {
      samples: result.fetchMilliseconds,
      median: median(result.fetchMilliseconds),
      max: Math.max(...result.fetchMilliseconds),
    },
    ...(result.release ? { release: result.release, fetchesBeforeRelease: result.beforeReleaseFetches } : {}),
    remainingStreamsAfterCleanup: result.statsAfterCleanup.activeSse + result.statsAfterCleanup.activeWs,
  })),
  lifecycle: results.filter((result) => !result.file.startsWith('compatibility-')),
  limitations: [
    'The local HTTP/1.1 fixture tests actual compiled clients, not real Runtime authorization.',
    'Old clients are the unchanged native EventSource behavior; old/new Runtimes model capability advertisement.',
    'Six SSE streams are admitted; two queued logical sources are cancelled before closing one occupied stream slot at approximately two seconds.',
    'Workbench reconnect ownership remains in ApiContext; its owner-level reconnect is outside this transport fixture.',
  ],
};
for (const result of results) assert.deepEqual(result.provenance, summary.provenance);
writeFileSync(destination, `${JSON.stringify(summary, null, 2)}\n`);
console.log(JSON.stringify({
  cases: summary.cases,
  browsers: summary.browsers,
  ws: summary.compatibility.filter((row) => row.transport !== 'sse')
    .map(({ browser, family, fetchMilliseconds }) => ({ browser, family, ...fetchMilliseconds })),
  sseFetchRangeMs: [
    Math.min(...summary.compatibility.filter((row) => row.transport === 'sse').map((row) => row.fetchMilliseconds.median)),
    Math.max(...summary.compatibility.filter((row) => row.transport === 'sse').map((row) => row.fetchMilliseconds.max)),
  ],
}, null, 2));
