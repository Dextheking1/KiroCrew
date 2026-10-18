/**
 * Shared Crew first submit wins: two verified principals send into one idle
 * session at the same time.
 *
 * Not part of the default gate, for the same reason as shared-crew-collab: it
 * needs a gateway with Shared Crew wired and two storage states for two
 * DIFFERENT principals. The integration harness runs it explicitly:
 *
 *   SHARED_CREW_STATE_A=<state-a.json> SHARED_CREW_STATE_B=<state-b.json> \
 *   SHARED_CREW_SESSION=<idle slot key> \
 *   PLAYWRIGHT_RUN_SHARED_CREW=1 npx playwright test shared-crew-send-race
 *
 * Every wait is on a visible state, never a timer.
 */
import { test, expect, type Browser, type Page } from '@playwright/test'

const STATE_A = process.env.SHARED_CREW_STATE_A || ''
const STATE_B = process.env.SHARED_CREW_STATE_B || ''
const SESSION = process.env.SHARED_CREW_SESSION || ''

async function open(browser: Browser, storageState: string): Promise<Page> {
  const context = await browser.newContext({ storageState })
  const page = await context.newPage()
  await page.goto(`/chat?slot=${encodeURIComponent(SESSION)}`)
  return page
}

test.describe('Shared Crew send race @shared-crew', () => {
  test.skip(!STATE_A || !STATE_B || !SESSION, 'needs two principals and an idle session')

  test('one send wins, the other keeps its draft and can Send now', async ({ browser }) => {
    const a = await open(browser, STATE_A)
    const b = await open(browser, STATE_B)
    const composerA = a.getByLabel('Message input')
    const composerB = b.getByLabel('Message input')
    // No input lock: both compose at once.
    await composerA.fill('first from A')
    await composerB.fill('first from B')
    // Each person sees the other typing before pressing, as two people racing
    // do. That line is also the sign each tab has finished handling its own
    // typing, so both presses land at the same moment; a press that lands after
    // the tab already showed the other's accepted send is a later send, not a
    // race, and is accepted by design.
    await expect(a.getByTestId('presence-typing')).toContainText(/typing/)
    await expect(b.getByTestId('presence-typing')).toContainText(/typing/)
    await Promise.all([composerA.press('Enter'), composerB.press('Enter')])

    // Exactly one tab shows the race notice; that tab keeps its draft.
    const noticeA = a.getByTestId('send-race-notice')
    const noticeB = b.getByTestId('send-race-notice')
    // A locator belongs to one page, so poll both pages for the one notice.
    await expect
      .poll(async () => (await noticeA.count()) + (await noticeB.count()))
      .toBe(1)
    const aLost = (await noticeA.count()) === 1
    const loser = aLost ? a : b
    const winner = aLost ? b : a
    await expect(winner.getByTestId('send-race-notice')).toHaveCount(0)
    await expect(loser.getByLabel('Message input')).toHaveValue(aLost ? 'first from A' : 'first from B')
    await expect(loser.getByTestId('send-race-notice')).toContainText(/sent first/)

    // Send now is a new send: it is accepted (running or queued), never a steer.
    await loser.getByTestId('send-race-send-now').click()
    await expect(loser.getByTestId('send-race-notice')).toHaveCount(0)
    await expect(loser.getByLabel('Message input')).toHaveValue('')

    // Reconnect reconciles from server truth: one row per accepted send, and
    // nothing is resubmitted by the reload.
    await loser.reload()
    await expect(loser.getByText(aLost ? 'first from A' : 'first from B', { exact: true })).toHaveCount(1)
    await expect(loser.getByText(aLost ? 'first from B' : 'first from A', { exact: true })).toHaveCount(1)
  })
})
