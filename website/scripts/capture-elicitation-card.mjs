/**
 * Screenshots of the MCP elicitation card in its three states (a form ask, a URL
 * ask, an answered row), light and dark, from the isolated capture entry
 * website/capture/elicitation-card.html.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort   # in another shell
 *   node scripts/capture-elicitation-card.mjs http://127.0.0.1:6814 ../temp-screenshots/elicitation-card
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/elicitation-card'
mkdirSync(OUT, { recursive: true })

const SCENES = ['form', 'url', 'resolved']
const THEMES = ['dark', 'light']

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
for (const scene of SCENES) {
  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: { width: 700, height: 520 } })
    page.on('pageerror', e => { console.error(`  pageerror: ${e.message}`); failures++ })
    await page.goto(`${BASE}/capture/elicitation-card.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
    const card = page.locator('[data-testid="elicitation-card"]')
    await card.waitFor()
    const buttons = await card.locator('button').count()
    if (scene === 'resolved' ? buttons !== 0 : buttons < 2) {
      console.error(`FAIL: scene=${scene} rendered ${buttons} button(s)`); failures++
    }
    const file = `${OUT}/elicitation-${scene}-${theme}.png`
    await page.locator('[data-capture-root]').screenshot({ path: file })
    console.log(`wrote ${file} (buttons=${buttons})`)
    await page.close()
  }
}
await browser.close()
process.exit(failures ? 1 : 0)
