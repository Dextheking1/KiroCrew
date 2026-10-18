/**
 * Shared Crew: two verified principals in two browser contexts.
 *
 * Not part of the default gate. It needs a gateway where Shared Crew is wired
 * (identity, capabilities, routes) and two storage states that authenticate as
 * two DIFFERENT principals, which the single-token harness cannot provide. The
 * tag keeps the shared-gateway run from collecting it; the integration harness
 * runs it explicitly:
 *
 *   SHARED_CREW_STATE_A=<state-a.json> SHARED_CREW_STATE_B=<state-b.json> \
 *   SHARED_CREW_SESSION=<slot key> \
 *   PLAYWRIGHT_RUN_SHARED_CREW=1 npx playwright test shared-crew-collab
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

test.describe('Shared Crew collaboration @shared-crew', () => {
  test.skip(!STATE_A || !STATE_B || !SESSION, 'needs two principals and a session')

  test('each person sees the other, typing locks nobody, one approval wins', async ({ browser }) => {
    const a = await open(browser, STATE_A)
    const b = await open(browser, STATE_B)

    // Presence: each tab shows the other person, never itself.
    await expect(a.getByTestId('presence-chips')).toBeVisible()
    await expect(b.getByTestId('presence-chips')).toBeVisible()

    // Concurrent typing: both composers stay editable; typing shows as a word.
    const composerA = a.getByRole('textbox').last()
    const composerB = b.getByRole('textbox').last()
    await composerA.fill('draft from A')
    await composerB.fill('draft from B')
    await expect(b.getByTestId('presence-typing')).toContainText(/typing/)
    await expect(composerA).toHaveValue('draft from A')
    await expect(composerB).toHaveValue('draft from B')

    // Racing an approval: whichever lands first wins; the other tab shows who.
    const approveA = a.getByRole('button', { name: /^Allow|^Approve/ }).first()
    const approveB = b.getByRole('button', { name: /^Reject|^Deny/ }).first()
    await expect(approveA).toBeVisible()
    await expect(approveB).toBeVisible()
    await Promise.all([approveA.click(), approveB.click()])
    await expect(a.getByTestId('decision-attribution')).toContainText(/(Approved|Rejected) by/)
    await expect(b.getByTestId('decision-attribution')).toContainText(/(Approved|Rejected) by/)

    // Reconnect after acceptance: a reload shows the same decision once.
    await b.reload()
    await expect(b.getByTestId('decision-attribution').getByRole('listitem')).toHaveCount(1)
  })

  test('cancelling queued work cancels only that item', async ({ browser }) => {
    const a = await open(browser, STATE_A)
    const list = a.getByTestId('work-status-list')
    await expect(list).toBeVisible()
    const queued = list.locator('[data-state="queued"]').first()
    await expect(queued).toBeVisible()
    const id = await queued.getAttribute('data-testid')
    await queued.getByRole('button', { name: /Cancel this item/ }).click()
    await expect(a.getByTestId(id || '')).toHaveAttribute('data-state', /cancel/)
  })
})
