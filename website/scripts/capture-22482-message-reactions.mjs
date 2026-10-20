/**
 * Screenshots + assertions for the message reaction row (#22482).
 *
 * Drives capture/message-reactions.html: the REAL UserMessage/AssistantMessage
 * rows against fixed local state (no gateway). Two scenes per theme — idle
 * (row hidden, nothing painted by default) and active (a reaction already
 * placed, stays visible without hover) — so the PR shows both the "nothing
 * extra most of the time" default and the marker use case the issue asks for.
 *
 * A `hover` scene was tried and dropped: this harness's synthetic
 * `page.mouse.move` does not register CSS `:hover` on the pre-existing
 * `data-message-actions` row either (confirmed against main, same opacity-0
 * result), so it is an environment limitation of this isolated capture page,
 * not something to assert about the reaction row specifically. The real hover
 * reveal is exercised by jsdom-based `fireEvent`/class-presence tests instead
 * (`MessageReactions.wiring.test.tsx`), which do not depend on the browser's
 * live hover state.
 *
 * Asserted, not assumed, per scene: the fixed four emoji render as inline
 * one-click toggle buttons, an active emoji carries aria-pressed="true", and an
 * idle row's bar is not painted (opacity 0).
 *
 * Starts its own vite dev server on a loopback ephemeral port.
 *
 * Usage:
 *   node scripts/capture-22482-message-reactions.mjs ../temp-screenshots/22482-message-reactions
 */
import { chromium } from 'playwright'
import { spawn } from 'node:child_process'
import { mkdirSync, statSync } from 'node:fs'
import { once } from 'node:events'

const OUT = process.argv[2] || '../temp-screenshots/22482-message-reactions'
const PORT = Number(process.argv[3]) || 6841
mkdirSync(OUT, { recursive: true })

const BASE = `http://127.0.0.1:${PORT}`
const READY_URL = `${BASE}/capture/message-reactions.html?theme=dark&scene=idle`
const BOOT_TIMEOUT_MS = 90_000
const POLL_INTERVAL_MS = 300

const sleep = ms => new Promise(r => setTimeout(r, ms))

/** Spawn the vite dev server, capturing its combined output for error text. */
function spawnVite() {
  const proc = spawn('npx', ['vite', '--host', '127.0.0.1', '--port', String(PORT), '--strictPort'], {
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const chunks = []
  const collect = c => chunks.push(String(c))
  proc.stdout.on('data', collect)
  proc.stderr.on('data', collect)
  return { proc, readLog: () => chunks.join('') }
}

/** Poll READY_URL until it answers 200, the server exits, or the boot budget
 *  elapses — whichever comes first. */
async function waitForReady(proc, readLog) {
  for (let elapsed = 0; elapsed < BOOT_TIMEOUT_MS; elapsed += POLL_INTERVAL_MS) {
    if (proc.exitCode !== null) throw new Error(`vite exited (${proc.exitCode}):\n${readLog()}`)
    try {
      if ((await fetch(READY_URL)).ok) return
    } catch { /* server not listening yet */ }
    await sleep(POLL_INTERVAL_MS)
  }
  proc.kill('SIGKILL')
  throw new Error(`vite did not answer ${READY_URL} within ${BOOT_TIMEOUT_MS}ms:\n${readLog()}`)
}

const { proc, readLog } = spawnVite()
await waitForReady(proc, readLog)
const base = BASE
const pageErrors = []
let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const browser = await chromium.launch()
try {
  // `shown` counts buttons actually PAINTED (opacity > 0). Every button keeps
  // its slot in the row (reserved space, no layout shift); an unplaced one is
  // transparent at rest and opaque on hover, so opacity — not display — is what
  // distinguishes the states.
  const shownCount = async locator => locator.evaluateAll(
    els => els.filter(el => Number(getComputedStyle(el).opacity) > 0).length,
  )

  for (const theme of ['dark', 'light']) {
    for (const scene of ['idle', 'active', 'hover', 'error']) {
      const name = `${scene}-${theme}`
      const page = await browser.newPage({ viewport: { width: 820, height: 420 }, deviceScaleFactor: 2 })
      page.on('pageerror', e => pageErrors.push(String(e)))
      await page.goto(`${base}/capture/message-reactions.html?theme=${theme}&scene=${scene}`, { waitUntil: 'networkidle' })
      const root = page.locator('[data-capture-root]')
      await root.waitFor()

      const bars = page.getByTestId('message-reaction-bar')
      const barCount = await bars.count()
      if (barCount !== 2) fail(`${name}: expected 2 reaction bars (user + assistant), found ${barCount}`)

      const assistantBar = bars.nth(1)
      // All four toggle buttons are always in the DOM (one-click targets); how
      // many are PAINTED depends on the scene.
      const triggers = assistantBar.getByTestId('reaction-trigger')
      if ((await triggers.count()) !== 4) fail(`${name}: expected 4 inline reaction buttons in the DOM, found ${await triggers.count()}`)

      if (scene === 'idle') {
        // No reaction, no hover: the row reserves its slot but paints no button
        // (all transparent), the same way the action strip is present-but-faded.
        const shown = await shownCount(triggers)
        if (shown !== 0) fail(`${name}: idle row should paint 0 buttons, painted ${shown}`)
      }
      if (scene === 'active') {
        // 👍 placed on the assistant reply: exactly the placed button is opaque
        // at rest (the marker), its aria-pressed is true, and the other three
        // hold their slots transparent until hover (no layout shift).
        const shown = await shownCount(triggers)
        if (shown !== 1) fail(`${name}: a reacted row at rest should paint only the placed emoji, painted ${shown}`)
        const pressed = await triggers.nth(0).getAttribute('aria-pressed')
        if (pressed !== 'true') fail(`${name}: 👍 should be aria-pressed on the assistant reply, got ${pressed}`)
      }
      if (scene === 'hover') {
        // The reveal fades in the full fixed set so the first reaction is one click.
        const shown = await shownCount(triggers)
        if (shown !== 4) fail(`${name}: hover should reveal all 4 reaction buttons, painted ${shown}`)
      }
      if (scene === 'error') {
        // The inline failure notice renders under the assistant reply.
        const notice = assistantBar.getByText('Could not add the reaction. Try again.')
        if ((await notice.count()) !== 1) fail(`${name}: expected the inline reaction error notice, found ${await notice.count()}`)
      }

      const file = `${OUT}/${name}.png`
      await root.screenshot({ path: file })
      const bytes = statSync(file).size
      if (bytes < 1500) fail(`${name}: capture is only ${bytes} bytes`)
      console.log(`${name}: captured ${bytes} bytes`)
      await page.close()
    }
  }
} finally {
  await browser.close()
  proc.kill('SIGKILL')
  await once(proc, 'exit').catch(() => {})
}

if (pageErrors.length) { console.error('page errors:', pageErrors); failures++ }
if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN')
process.exit(0)
