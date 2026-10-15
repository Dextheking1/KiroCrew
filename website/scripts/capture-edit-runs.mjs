/**
 * Screenshot harness for edit runs: consecutive edits to one file render as ONE
 * row (editRuns.ts), the file name is shown decoded, and only once.
 *
 * Reproduces the reported transcript: a read, then three edits to a file whose
 * path the transport spelled URL-encoded (`The%20Power%20of…`), then a reply.
 * A fourth edit to a different file follows the reply, to show that a visible
 * row between edits keeps them apart.
 *
 * Runs the REAL built SPA (website/dist) behind the shared transcript harness
 * with every /api/** call answered from fixtures.
 *
 * Usage: node scripts/capture-edit-runs.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/edit-runs'
const SLOT = 'chat-edit-runs'
const PROJECT = '/home/user/workspace/blog'
const BLOG = 'drafts/The%20Power%20of%20Running%20Remote%20Blog.md'

mkdirSync(OUT, { recursive: true })

const diff = (path, start, removed, added) => [
  `--- ${path}`, `+++ ${path}`, `@@ -${start},${removed.length + 2} +${start},${added.length + 2} @@`,
  ' Running a remote crew changes how a team works.',
  ...removed.map(l => `-${l}`), ...added.map(l => `+${l}`),
  ' The rest of this post walks through it.',
].join('\n')

const t0 = Date.now() / 1000 - 900
const tool = (id, ts, input, kind = 'edit', content = '🔧 fs_write') => ({
  role: 'tool', ts: t0 + ts, content, cls: '', meta: { tool_call_id: id, kind, input },
})
const ok = (id, ts) => ({ role: 'tool', ts: t0 + ts, content: '✅ fs_write', cls: '', meta: { tool_call_id: id } })

const messages = [
  { role: 'user', ts: t0, content: 'Tighten the intro of the remote blog draft, and keep my voice.' },
  tool('tc_read', 4, JSON.stringify({ operations: [{ mode: 'Line', path: `${PROJECT}/drafts/The Power of Running Remote Blog.md` }] }), 'read', '🔧 fs_read'),
  ok('tc_read', 5),
  tool('tc_e1', 10, diff(BLOG, 3, ['Remove this and', 'Fly', 'the second', 'paragraph'], [])),
  ok('tc_e1', 11),
  tool('tc_e2', 14, diff(BLOG, 9, ['It is fast.'], ['It is fast, and it is yours.'])),
  ok('tc_e2', 15),
  tool('tc_e3', 18, diff(BLOG, 14, ['So try it.'], ['So try it this week.'])),
  ok('tc_e3', 19),
  { role: 'assistant', ts: t0 + 24, content: 'Trimmed the intro to the two lines that carry it, kept your phrasing, and sharpened the close.' },
  tool('tc_e4', 30, diff('drafts/outline.md', 1, ['- intro'], ['- intro (done)'])),
  ok('tc_e4', 31),
  { role: 'assistant', ts: t0 + 34, content: 'Marked the intro done in the outline.' },
]

const slots = [{
  key: SLOT, title: 'Tighten the remote blog intro', running: false, last_message: 'Marked the intro done.',
  messages: messages.length, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const detail = { running: false, has_more: false, total: messages.length, queue: [], project: PROJECT, messages }

const CHIP_WAIT = { selector: '[data-testid="tool-diff-chip"]', settle: 900 }

async function main() {
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail, viewport: { width: 1280, height: 900 },
  })
  async function shot(name) {
    await page.mouse.move(0, 0)
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }
  await load('light', CHIP_WAIT)
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForSelector(CHIP_WAIT.selector, { timeout: 20000 })
  await page.waitForTimeout(900)
  // The turn's tool rows fold behind "N tool calls" by default; open it.
  const toggle = page.locator('button[aria-expanded="false"]', { hasText: /tool call/ }).first()
  if (await toggle.count()) { await toggle.evaluate(el => el.click()); await page.waitForTimeout(700) }
  const chips = await page.locator('[data-testid="tool-diff-chip"]').allTextContents()
  const labels = await page.locator('[data-testid="tool-pill-label"]').allTextContents()
  console.log('chips', JSON.stringify(chips))
  console.log('labels', JSON.stringify(labels))
  await shot('01-collapsed-run')
  await page.locator('[data-testid="tool-diff-chip"]').first().evaluate(el => { el.scrollIntoView({ block: 'start' }); el.click() })
  await page.waitForTimeout(900)
  console.log('open cards', await page.locator('.diff-block').count())
  await shot('02-run-open')
  await close()
}

main().catch(err => { console.error(err); process.exit(1) })
