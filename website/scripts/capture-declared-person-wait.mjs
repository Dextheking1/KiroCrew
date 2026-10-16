/**
 * Screenshot harness + assertions for a legacy loop whose agent DECLARED it is
 * waiting on a person (`monitor_update(waiting_on_person=true)`).
 *
 * One frame: the goal popover on an active loop carrying `waiting_on_person` and
 * `person_wait_declared`. Its status line says the agent asked a question in this
 * chat and asks for a reply there or a press of Nudge now, the Nudge now control
 * shows its name beside its icon, and it is ENABLED even though the loop's
 * deadline is long past -- that press is one of the ways a person releases the
 * hold, so a disabled button would leave the copy pointing at a dead control.
 *
 * It drives the REAL built SPA (website/dist) behind `serveDist` with every /api/**
 * call answered from fixtures by `stubDashboardApi` -- no gateway, no dashboard
 * auth, no kiro-cli -- and exits non-zero unless the frame renders what it claims,
 * so a stale bundle reds instead of photographing the old copy. Labels come from
 * the catalog, so a key rename breaks the capture loudly.
 *
 * Usage: node scripts/capture-declared-person-wait.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/declared-person-wait'
const SLOT = 'chat-loop'
const PROJECT = '/home/user/workspace/uploader'
const LOOP_ID = 'loop-declared-wait'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const STATUS = manual.components.autoNudgePopover.waiting_on_person_declared_hold
const NUDGE_NOW = manual.components.autoNudgePopover.nudge_now
if (!STATUS || !NUDGE_NOW) {
  throw new Error('components.autoNudgePopover declared-hold / nudge_now keys missing -- renamed?')
}

/** Fixed instants, so a re-capture produces the same bytes. */
const FIXED_FIRE_TS = Date.UTC(2026, 9, 16, 22, 40, 0) / 1000
const FIXED_HOLD_TS = Date.UTC(2026, 9, 16, 22, 45, 0) / 1000
const NOW = Math.floor(Date.UTC(2026, 9, 16, 23, 0, 0) / 1000)

const slots = [{
  key: SLOT,
  title: 'Drive PR 21622 to green',
  running: false,
  last_message: 'Asked you to approve the force-push of 3f9c2ab.',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: NOW,
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: NOW - 900, content: 'Babysit PR 21622 until it is green.' },
    { role: 'assistant', ts: NOW - 120, content: 'The rebase is ready. Approve the force-push of 3f9c2ab?' },
  ],
}

/** One loop, shaped like the backend's `asdict(loop)`, held by its agent's declaration. */
const loop = {
  id: LOOP_ID,
  slot_key: SLOT,
  message: 'Check https://github.com/kirodotdev/KiroCrew/pull/21622 for new CI results and review comments.',
  idle_secs: 300,
  max_cycles: 24,
  cycle_count: 3,
  active: true,
  last_fire_ts: FIXED_FIRE_TS,
  // A deadline long past: the hold fires nothing, so the deadline is never met.
  next_due_ts: 1,
  waiting_on_person: true,
  waiting_on_person_at: FIXED_HOLD_TS,
  person_wait_declared: true,
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })

const results = []
const check = (name, ok, detailText) => {
  results.push({ name, ok })
  if (!ok) console.error(`FAIL ${name}: ${detailText}`)
}

try {
  const page = await context.newPage()
  logPageProblems(page)
  const extra = async (path, route) => {
    if (path === `/api/autonudge/slot/${SLOT}`) { await json(route, { loop }); return true }
    if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [loop] }); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }
  await stubDashboardApi(page, {
    slots,
    extra,
    localStorageEntries: { 'mc-active-slot': SLOT, 'mc-lang': 'en' },
  })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  const chip = page.getByRole('button', { name: /^Goal active \(cycle / }).first()
  await chip.waitFor({ state: 'visible', timeout: 15000 })
  await chip.click()
  const popover = page.getByRole('dialog').filter({ hasText: 'Set a goal' }).first()
  await popover.waitFor({ state: 'visible', timeout: 10000 })
  // Radix plays a zoom/fade entry animation; shoot after it settles.
  await page.waitForTimeout(700)

  const status = (await popover.getByTestId('auto-nudge-status').innerText()).trim()
  check('status line is the declared-hold copy', status === STATUS,
    `status reads ${JSON.stringify(status)}, want ${JSON.stringify(STATUS)}`)
  const nudge = popover.getByRole('button', { name: NUDGE_NOW })
  check('one Nudge now control', (await nudge.count()) === 1, `found ${await nudge.count()}`)
  check('Nudge now is enabled', await nudge.isEnabled(), 'Nudge now is disabled on a declared hold')
  const visible = (await nudge.getByTestId('auto-nudge-play-label').innerText().catch(() => '')).trim()
  check('Nudge now names itself beside its icon', visible === NUDGE_NOW,
    `visible label reads ${JSON.stringify(visible)}, want ${JSON.stringify(NUDGE_NOW)}`)

  const popoverOut = join(OUT, '01-declared-hold-popover.png')
  await popover.screenshot({ path: popoverOut })
  console.log('wrote', popoverOut)
  await page.close()
} finally {
  await browser.close()
  srv.close()
}

const failed = results.filter(r => !r.ok)
console.log(`${results.length - failed.length}/${results.length} checks passed`)
if (failed.length) process.exit(1)
