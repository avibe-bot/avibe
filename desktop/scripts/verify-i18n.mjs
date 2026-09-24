import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const expectedNoticeCodes = [
  'adopted',
  'invalid_origin',
  'launcher_exited',
  'probing',
  'ready',
  'ready_timeout',
  'runtime_discovery_failed',
  'runtime_install_failed',
  'runtime_not_found',
  'runtime_ownership_lost',
  'runtime_spawn_failed',
  'runtime_stopped',
  'starting',
  'workbench_navigation_failed',
]

async function catalog(locale) {
  const source = await readFile(
    new URL(`../../ui/src/i18n/${locale}.json`, import.meta.url),
    'utf8',
  )
  return JSON.parse(source).desktopBootstrap
}

function leafShape(value, prefix = '') {
  return Object.entries(value)
    .flatMap(([key, child]) => {
      const path = prefix ? `${prefix}.${key}` : key
      return child && typeof child === 'object'
        ? leafShape(child, path)
        : [path]
    })
    .sort()
}

function placeholders(value) {
  return [...value.matchAll(/\{\{([a-zA-Z0-9_]+)\}\}/g)]
    .map((match) => match[1])
    .sort()
}

const [en, zh] = await Promise.all([catalog('en'), catalog('zh')])

assert.deepEqual(leafShape(zh), leafShape(en), 'desktop locale keys must match')
assert.deepEqual(
  Object.keys(en.notices).sort(),
  expectedNoticeCodes,
  'notice catalog must exactly match the frozen Rust contract',
)
assert.deepEqual(Object.keys(zh.notices).sort(), expectedNoticeCodes)

const notificationKeys = [
  'approvalRequested.body',
  'approvalRequested.title',
  'runCanceled.body',
  'runCanceled.title',
  'runFailed.body',
  'runFailed.title',
  'runSucceeded.body',
  'runSucceeded.title',
  'toggle',
]
assert.deepEqual(leafShape(en.notifications), notificationKeys)
assert.deepEqual(leafShape(zh.notifications), notificationKeys)
for (const key of notificationKeys) {
  const english = key.split('.').reduce((value, segment) => value[segment], en.notifications)
  const chinese = key.split('.').reduce((value, segment) => value[segment], zh.notifications)
  assert.ok(english.length > 0 && chinese.length > 0)
  assert.deepEqual(placeholders(chinese), placeholders(english), `placeholder mismatch for notifications.${key}`)
}

for (const code of expectedNoticeCodes) {
  assert.deepEqual(
    placeholders(zh.notices[code]),
    placeholders(en.notices[code]),
    `placeholder mismatch for ${code}`,
  )
}
