// @vitest-environment jsdom
/**
 * project-city against project-report, on one record.
 *
 * project-city draws project-report's record as a city and carries that page's four
 * rules in a script of its own: a cost nobody reported is a dash, a cut list says how
 * much it dropped, a worker that served two tasks is billed once, and a verdict written
 * before the record last advanced reads as no word. Two copies of a rule can drift, so
 * each case mounts BOTH shipped pages on the same field bag (the sample record the
 * evidence screenshots are taken from) and holds the city's figures to the report's:
 * the header sentence, the three counts and the figures under them, the spend line,
 * every row of the work tree, each cut count and the verdict's state. Figures and states
 * are compared, not labels, because the two pages word some labels differently.
 *
 * Both pages run in this one window and each registers listeners on it, so no case
 * here dispatches an event or posts a message: a page's listener would redraw into the
 * other page's markup. A pill click is a plain DOM click and reaches only the page
 * that drew the pill.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const BUILTIN = resolve(__dirname, '../../../src/kiro_crew/dashboard_templates/builtin')
const FIXTURE = resolve(__dirname, '../../../test/fixtures/dashboard_templates/sample_workstreams.json')

interface Sample {
  agentic: Record<string, unknown>
  agentic_written_at: Record<string, string>
  folds: { workstreams: { seq: number; value: Record<string, unknown> } }
  now: string
}
const SAMPLE = JSON.parse(readFileSync(FIXTURE, 'utf-8')) as Sample
const NOW = Date.parse(SAMPLE.now)
const RECORD = SAMPLE.folds.workstreams.value

type Id = 'project-report' | 'project-city'

/** Where each page puts the figures compared here. */
const SEL: Record<Id, Record<string, string>> = {
  'project-report': {
    p: 'pr', sentence: '#pr-sentence', kpi: '.pr-kpi', kpiValue: '.pr-kpi-v', kpiSub: '.pr-kpi-sub',
    cost: '.pr-cost-l', verdict: '.pr-vd-state', card: '.pr-card', cardTitle: ':scope > .pr-card-h > .pr-card-t',
  },
  'project-city': {
    p: 'pc', sentence: '#pc-sentence', kpi: '.pc-kpi', kpiValue: '.pc-kpi-v', kpiSub: '.pc-kpi-s',
    cost: '.pc-cost-l', verdict: '.pc-verdict-state', card: '.pc-panel', cardTitle: ':scope > .pc-ph > .pc-pt',
  },
}

/**
 * A shipped page split by the DOM rather than by a pattern: its markup with every script
 * element removed, and its last script, which is the page's own. project-city's first
 * script is the 3D library; without it the city says so and the HTML half draws alone.
 */
function load(id: Id) {
  const doc = new DOMParser().parseFromString(readFileSync(resolve(BUILTIN, id, 'template.html'), 'utf-8'), 'text/html')
  const scripts = Array.from(doc.querySelectorAll('script'))
  const page = scripts[scripts.length - 1]?.textContent ?? ''
  for (const node of scripts) node.remove()
  return { body: doc.body, page }
}
const PAGES: Record<Id, { body: HTMLElement; page: string }> = {
  'project-report': load('project-report'),
  'project-city': load('project-city'),
}

/** The sample's field bag as the host hands it to either page: the seven fields both bind. */
function bag(over: Record<string, unknown> = {}) {
  return {
    items: RECORD.items,
    omitted: RECORD.omitted,
    series: RECORD.series,
    last_entry_at: RECORD.last_entry_at,
    for_you: SAMPLE.agentic.for_you,
    verdict: SAMPLE.agentic.verdict,
    ci: SAMPLE.agentic.ci,
    ...over,
  }
}

function mount(id: Id, fields: Record<string, unknown>, writtenAt: Record<string, string>) {
  ;(window as unknown as { kirocrew: unknown }).kirocrew = {
    fields: JSON.parse(JSON.stringify(fields)),
    agentic: ['ci', 'for_you', 'verdict'],
    seq: SAMPLE.folds.workstreams.seq,
    stale: false,
    missing: [],
    written_at: { ...writtenAt },
  }
  const { body, page } = PAGES[id]
  document.body.replaceChildren(...Array.from(body.childNodes, (node) => document.importNode(node, true)))
  new Function(page)()
}

const squash = (s: string | null | undefined) => (s ?? '').replace(/\s+/g, ' ').trim()
/** The text a node carries itself, without its child elements' text. */
const ownText = (node: Element | null) =>
  squash(Array.from(node?.childNodes ?? []).filter((n) => n.nodeType === 3).map((n) => n.textContent).join(''))
/** Every number a label states, in order: what is compared where the two pages word a label differently. */
const figures = (s: string | null | undefined) => (squash(s).match(/\d+(?:\.\d+)?/g) ?? []).map(Number)
/** The leading count of a "N ... not shown" line, or null when there is none. */
const leading = (node: Element | null | undefined) => {
  const m = /^(\d+)/.exec(squash(node?.textContent))
  return m ? Number(m[1]) : null
}

/** What one page states about the record, in figures. */
function reading(id: Id) {
  const s = SEL[id]
  const p = s.p
  const q = (sel: string) => document.querySelector(sel)
  const desk = Array.from(document.querySelectorAll(s.card)).find(
    (card) => squash(card.querySelector(s.cardTitle)?.textContent) === 'Needs you',
  )
  const cost = q(s.cost)
  return {
    sentence: Array.from(q(s.sentence)?.querySelectorAll('b') ?? []).map((b) => squash(b.textContent)),
    kpis: Array.from(document.querySelectorAll(s.kpi)).map((card) => [
      ownText(card.querySelector(s.kpiValue)),
      figures(card.querySelector(s.kpiSub)?.textContent),
    ]),
    spent: squash(cost?.querySelector('b')?.textContent),
    trail: squash(cost?.querySelector(`.${p}-cost-t`)?.textContent),
    rows: Array.from(document.querySelectorAll(`.${p}-n`)).map((row) => {
      const level = /\b(?:pr|pc)-n-(epic|story|task|sub)\b/.exec(row.className)?.[1] ?? '?'
      const head = `:scope > .${p}-n-h`
      return [
        level,
        squash(row.querySelector(`${head} .${p}-n-t`)?.textContent),
        squash(row.querySelector(`${head} .${p}-n-done`)?.textContent),
        squash(row.querySelector(`${head} .${p}-n-cr`)?.textContent),
        leading(row.querySelector(`:scope > .${p}-kids > .${p}-held`)),
      ]
    }),
    deskCut: leading(desk?.querySelector(`:scope > .${p}-held`)),
    omitted: leading(q('[data-dashboard-field="omitted"]')),
    verdict: squash(q(s.verdict)?.textContent),
  }
}

function both(fields: Record<string, unknown>, writtenAt: Record<string, string>, then?: (id: Id) => void) {
  const out = {} as Record<Id, ReturnType<typeof reading>>
  for (const id of ['project-report', 'project-city'] as const) {
    mount(id, fields, writtenAt)
    then?.(id)
    out[id] = reading(id)
  }
  return out
}

beforeEach(() => {
  // The clock only, pinned to the sample's own "now", so both pages read one instant.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('project-city states the record the way project-report does', () => {
  it('gives the same sentence, counts, spend and tree on the all-workstreams view', () => {
    const r = both(bag(), SAMPLE.agentic_written_at)
    const report = r['project-report']
    const city = r['project-city']
    expect(report.sentence).toHaveLength(3)
    expect(city.sentence).toEqual(report.sentence)
    expect(report.kpis).toHaveLength(3)
    expect(city.kpis).toEqual(report.kpis)
    expect(city.spent).toEqual(report.spent)
    expect(city.trail).toEqual(report.trail)
    expect(report.rows.length).toBeGreaterThan(10)
    expect(city.rows).toEqual(report.rows)
  })

  it('draws the same dash for a cost nobody reported', () => {
    const r = both(bag(), SAMPLE.agentic_written_at)
    const unpriced = (rows: unknown[][]) => rows.filter((row) => row[3] === '\u2014').map((row) => row[1])
    expect(unpriced(r['project-report'].rows).length).toBeGreaterThan(0)
    expect(unpriced(r['project-city'].rows)).toEqual(unpriced(r['project-report'].rows))
  })

  it('names a board that reported no cost beside the total, by the same count', () => {
    // The sample's last board as one that never reported its cost.
    const items = (RECORD.items as Array<Record<string, unknown>>).map((b, i, list) =>
      i === list.length - 1 ? { ...b, credits: 0, credits_reported: false } : b,
    )
    const r = both(bag({ items }), SAMPLE.agentic_written_at)
    expect(r['project-report'].trail).toContain('1 reported no cost')
    expect(r['project-city'].trail).toBe(r['project-report'].trail)
    expect(r['project-city'].spent).toBe(r['project-report'].spent)
    expect(r['project-city'].sentence).toEqual(r['project-report'].sentence)
  })

  it('bills the worker that served two tasks once, on the same row', () => {
    const r = both(bag(), SAMPLE.agentic_written_at)
    const parent = (rows: unknown[][]) => rows.find((row) => String(row[1]).startsWith('project-report template'))
    // 7.31 of its own, and 2.60 + 1.40 + 1.30 under it: worker 8 ran two of those
    // subtasks at 1.30 each and is billed once (12.61). Billed twice it would be 13.9.
    expect(parent(r['project-report'].rows)?.[3]).toBe('12.6')
    expect(parent(r['project-city'].rows)).toEqual(parent(r['project-report'].rows))
  })

  it('says how much each cut list dropped, by the same counts', () => {
    const r = both(bag({ omitted: 2 }), SAMPLE.agentic_written_at)
    const report = r['project-report']
    const city = r['project-city']
    expect(report.omitted).toBe(2)
    expect(city.omitted).toBe(report.omitted)
    // The board whose task list the fold cut, and the Needs-you items it could not carry.
    expect(report.rows.some((row) => row[4] === 2)).toBe(true)
    expect(city.rows.map((row) => row[4])).toEqual(report.rows.map((row) => row[4]))
    expect(report.deskCut).toBe(1)
    expect(city.deskCut).toBe(report.deskCut)
  })

  it('lets a verdict stand while the record has not moved since it was written', () => {
    const r = both(bag(), SAMPLE.agentic_written_at)
    expect(r['project-report'].verdict).toBe('Needs you')
    expect(r['project-city'].verdict).toBe(r['project-report'].verdict)
  })

  it('reads the same verdict as no word once the record advanced after it', () => {
    const r = both(bag(), { verdict: '2026-10-04T17:00:00Z' })
    expect(r['project-report'].verdict).toBe('No word yet')
    expect(r['project-city'].verdict).toBe(r['project-report'].verdict)
  })

  it('gives the same sentence and tree under one picked workstream', () => {
    const goal = String((RECORD.items as Array<{ goal: string }>)[0].goal)
    const r = both(bag(), SAMPLE.agentic_written_at, (id) => {
      const pill = Array.from(document.querySelectorAll(`.${SEL[id].p}-pill`)).find((b) =>
        squash(b.textContent).startsWith(goal),
      ) as HTMLElement | undefined
      expect(pill, `${id} has a pill for "${goal}"`).toBeDefined()
      pill?.click()
    })
    const report = r['project-report']
    const city = r['project-city']
    expect(report.sentence).toHaveLength(3)
    expect(city.sentence).toEqual(report.sentence)
    expect(report.rows[0]?.[1]).toBe(goal)
    expect(city.rows).toEqual(report.rows)
  })
})
