/**
 * Screenshots + assertion for the ChatEmbed composer during a running turn, on
 * the isolated entry website/capture/embed-send-ws.html (the real ChatEmbed
 * behind the real scoped api, gateway-free).
 *
 * The slot read is a RUNNING turn. `POST /api/chat?ws=1` answers with the JSON
 * receipt the gateway returns at dispatch; the bare `POST /api/chat` is left
 * unanswered, which is what the gateway's SSE stream looks like until the turn
 * ends. The script types a message, sends it, waits, then shots the embed.
 *
 * Assertion (label `after` only): the composer is enabled again mid-turn.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-embed-send-ws.mjs http://127.0.0.1:6831 <out-dir> [label]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/embed-send-ws'
const LABEL = process.argv[4] || 'after'
const ASSERT = LABEL === 'after'
mkdirSync(OUT, { recursive: true })

const TRANSCRIPT = {
  messages: [
    { role: 'user', content: 'Summarise the open tickets for the example queue.', cls: '', ts: '2026-10-13T17:00:00Z' },
    { role: 'streaming', content: 'Reading the queue. 14 tickets so far, grouping them by owner', cls: '', ts: '2026-10-13T17:00:04Z' },
  ],
  running: true,
  title: 'Example app · ticket summary',
  has_more: false,
}

const browser = await chromium.launch()
let failures = 0
for (const [theme, w] of [['dark', 720], ['light', 720], ['dark', 360]]) {
  const page = await browser.newPage({ viewport: { width: w + 40, height: 560 } })
  await page.route('**/api/chat/slots/**', route => route.fulfill({ json: TRANSCRIPT }))
  await page.route(/\/api\/chat(\?.*)?$/, route => {
    if (new URL(route.request().url()).search === '?ws=1') {
      return route.fulfill({ json: { ok: true, slot: 'capture-app-embed' } })
    }
    // Bare route: the SSE stream that stays open until the turn ends.
    return new Promise(() => {})
  })
  await page.goto(`${BASE}/capture/embed-send-ws.html?theme=${theme}&w=${w}`, { waitUntil: 'load' })
  const input = page.locator('textarea[aria-label="Chat message"]')
  await input.waitFor({ timeout: 90000 })
  await input.fill('Also list which ones are past their due date.')
  await page.locator('button[aria-label="Send message"]').click()
  await page.waitForTimeout(800)
  const enabled = await input.isEnabled()
  if (enabled) await input.fill('And skip the ones already assigned.')
  await page.mouse.move(5, 5)
  console.log(`[${LABEL} ${theme} ${w}px] composer enabled mid-turn=${enabled}`)
  if (ASSERT && !enabled) failures++
  await page.locator('[data-capture-box]').screenshot({ path: `${OUT}/${LABEL}-${theme}-${w}.png` })
  await page.close()
}
await browser.close()
if (failures) {
  console.error(`FAIL: ${failures} capture(s) with a locked composer`)
  process.exit(1)
}
console.log('ALL GREEN')
