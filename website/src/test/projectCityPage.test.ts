// @vitest-environment jsdom
/**
 * The `project-city` page, driven rather than read.
 *
 * The page is a template the Python package ships: an inert body plus two scripts, the
 * three.js library and the page. Each case sets `window.kirocrew` the way the frame
 * does -- deep-frozen, so a page that wrote into the read would throw here -- inserts
 * the markup and runs the page script. jsdom has no WebGL, so the city takes its own
 * fallback and these cases hold the text half to project-report's rules: an absent cost
 * is a dash, a cut list says how much it dropped, a worker is billed once, a verdict
 * older than the record reads as no word, and the only message out is `act`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const TEMPLATE = resolve(__dirname, '../../../src/kiro_crew/dashboard_templates/builtin/project-city/template.html')
/**
 * The page split by the DOM rather than by a pattern: the parsed scripts' text (the
 * three.js library, then the page) and the parsed body with every script removed.
 */
const PARSED = new DOMParser().parseFromString(readFileSync(TEMPLATE, 'utf-8'), 'text/html')
const SCRIPT_NODES = Array.from(PARSED.querySelectorAll('script'))
const SCRIPTS = SCRIPT_NODES.map((node) => node.textContent ?? '')
for (const node of SCRIPT_NODES) node.remove()
const LIBRARY = SCRIPTS[0] as string
const PAGE = SCRIPTS[1] as string

/** The instant every case reads its elapsed figures against. */
const NOW = Date.parse('2026-10-04T17:12:00Z')
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString()
const CJK = /[\u3000-\u9fff\uff00-\uffef]/

function task(over: Record<string, unknown> = {}) {
  return {
    item_id: 'it_1', title: 'the fold', state: 'open', status: 'progress', summary: '', verdict: null, pr: null,
    round: 1, spender: 'w1', credits: 1, credits_reported: true, duration_ms: 60_000, last_report_at: iso(2),
    decision: '', events: [] as Array<Record<string, unknown>>, events_seen: 0,
    ...over,
  }
}

function board(tasks: Array<Record<string, unknown>>, over: Record<string, unknown> = {}) {
  return {
    id: 'b1', goal: 'ship the report', round: 1, parent: null, total: tasks.length, accepted: 0,
    open: tasks.length, needs_you: 0, credits: 1, credits_reported: true, last_activity_at: iso(3),
    tasks, tasks_omitted: 0, series: [],
    ...over,
  }
}

function freeze<T>(value: T): T {
  if (value === null || typeof value !== 'object') return value
  for (const key of Object.keys(value as object)) freeze((value as Record<string, unknown>)[key])
  return Object.freeze(value)
}

function read(fields: Record<string, unknown>, extra: { written_at?: Record<string, string>; locale?: string } = {}) {
  return freeze({
    fields,
    agentic: ['ci', 'for_you', 'verdict'],
    seq: 412,
    stale: false,
    missing: [],
    written_at: extra.written_at ?? {},
    ...(extra.locale === undefined ? {} : { locale: extra.locale }),
  })
}

/**
 * The listeners each mounted page put on `window`, taken off after its case. Without
 * that, a push in one case would also reach every page an earlier case mounted, and
 * each of those would redraw this case's markup from its own state.
 */
const listening: Array<[string, EventListenerOrEventListenerObject]> = []

function mount(fields: Record<string, unknown>, extra: { written_at?: Record<string, string>; locale?: string } = {}) {
  ;(window as unknown as { kirocrew: unknown }).kirocrew = read(fields, extra)
  document.body.replaceChildren(...Array.from(PARSED.body.childNodes, (node) => document.importNode(node, true)))
  const add = window.addEventListener.bind(window)
  const spy = vi.spyOn(window, 'addEventListener').mockImplementation((type, listener, options) => {
    if (listener) listening.push([type, listener])
    add(type, listener, options)
  })
  try {
    new Function(PAGE)()
  } finally {
    spy.mockRestore()
  }
}

/** A host push: a new read, then the event the host dispatches once it has filled. */
function push(fields: Record<string, unknown>, extra: { written_at?: Record<string, string>; locale?: string } = {}) {
  ;(window as unknown as { kirocrew: unknown }).kirocrew = read(fields, extra)
  window.dispatchEvent(new Event('kirocrew-dashboard'))
}

const text = (sel: string) => document.querySelector(sel)?.textContent?.replace(/\s+/g, ' ').trim() ?? ''
const all = (sel: string) => Array.from(document.querySelectorAll(sel))
const rowCredits = (title: string) => {
  const row = all('.pc-n-h').find((h) => h.querySelector('.pc-n-t')?.textContent === title)
  return row?.querySelector('.pc-n-cr')?.textContent ?? null
}

beforeEach(() => {
  // The clock only: the page's own timers stay real.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
})
afterEach(() => {
  for (const [type, listener] of listening.splice(0)) window.removeEventListener(type, listener)
  vi.useRealTimers()
  vi.restoreAllMocks()
  delete (window as unknown as { THREE?: unknown }).THREE
})

describe('project-city: the page the gateway ships', () => {
  it('carries the 3D library and the page as its two scripts', () => {
    expect(SCRIPTS).toHaveLength(2)
    expect(LIBRARY).toContain('SPDX-License-Identifier: MIT')
    expect(PAGE).toContain("'kirocrew-dashboard:act'")
  })

  it('binds every field to an element the page then rewrites as words', () => {
    mount({ items: [board([task()])], omitted: 2, series: [], last_entry_at: iso(1) })
    const note = text('#pc-note')
    expect(note).toContain('1 workstream,')
    expect(note).toContain('2 more not shown')
    expect(note).not.toContain('[{')
  })

  describe('without WebGL', () => {
    it('says the city cannot be drawn and keeps every number', () => {
      new Function(LIBRARY)()
      const THREE = (window as unknown as { THREE: Record<string, unknown> }).THREE
      expect(typeof THREE.WebGLRenderer).toBe('function')
      expect(typeof THREE.OrbitControls).toBe('function')
      vi.spyOn(console, 'error').mockImplementation(() => {})
      vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
      mount({ items: [board([task()])], omitted: 0, series: [], last_entry_at: iso(1) })
      expect(document.getElementById('pc-city')?.getAttribute('data-gl')).toBe('off')
      expect(text('.pc-nogl')).toContain('WebGL')
      expect(all('.pc-kpi')).toHaveLength(3)
      expect(document.getElementById('pc-ctrl')?.hidden).toBe(true)
    })

    it('says so when the library did not load at all', () => {
      mount({ items: [board([task()])], omitted: 0, series: [], last_entry_at: iso(1) })
      expect(text('.pc-nogl')).toContain('did not load')
    })
  })

  describe('an absent cost is a dash, never a zero', () => {
    it('draws a dash for a task and a board nobody measured', () => {
      const t = task({ credits: 0, credits_reported: false, title: 'unpriced' })
      mount({ items: [board([t], { credits: 0, credits_reported: false })], omitted: 0, series: [], last_entry_at: iso(1) })
      expect(rowCredits('unpriced')).toBe('\u2014')
      expect(text('#pc-sentence')).toContain('\u2014 credits')
    })

    it('names the boards that reported no cost beside the total', () => {
      const a = board([task({ item_id: 'a' })], { id: 'b1', credits: 4, credits_reported: true })
      const b = board([task({ item_id: 'b', spender: 'w2' })], { id: 'b2', goal: 'other', credits: 0, credits_reported: false })
      mount({ items: [a, b], omitted: 0, series: [{ hour: '2026-10-04T16:00Z', credits: 4, accepted: 0 }], last_entry_at: iso(1) })
      expect(text('.pc-cost')).toContain('1 reported no cost')
    })
  })

  describe('a cut list says how much it dropped', () => {
    it('counts the tasks a board did not carry', () => {
      mount({ items: [board([task()], { tasks_omitted: 3 })], omitted: 0, series: [], last_entry_at: iso(1) })
      expect(document.body.textContent).toContain('3 more tasks not shown')
    })

    it('counts the boards the record did not carry', () => {
      mount({ items: [board([task()])], omitted: 2, series: [], last_entry_at: iso(1) })
      expect(document.body.textContent).toContain('2 more workstreams not shown')
    })
  })

  it('bills a worker that served two tasks once', () => {
    const tasks = [
      task({ item_id: 'a', title: 'first', spender: 'w1', credits: 5 }),
      task({ item_id: 'b', title: 'second', spender: 'w1', credits: 5 }),
    ]
    mount({ items: [board(tasks, { goal: 'one worker', credits: 5 })], omitted: 0, series: [], last_entry_at: iso(1) })
    expect(rowCredits('one worker')).toBe('5.00')
    expect(text('.pc-rings')).toContain('together 5.00 credits')
  })

  describe('the verdict', () => {
    const verdict = { state: 'on_track', headline: 'all moving', blocker: '' }

    it('stands while the record has not moved since it was written', () => {
      mount({ items: [board([task()])], omitted: 0, series: [], last_entry_at: iso(5), verdict },
        { written_at: { verdict: iso(1) } })
      expect(text('.pc-verdict-state')).toBe('On track')
      expect(text('.pc-verdict-head')).toBe('all moving')
    })

    it('reads as no word once the record advanced after it', () => {
      mount({ items: [board([task()])], omitted: 0, series: [], last_entry_at: iso(1), verdict },
        { written_at: { verdict: iso(5) } })
      expect(text('.pc-verdict-state')).toBe('No word yet')
      expect(text('.pc-verdict')).toContain('Its last word was "all moving"')
    })
  })

  it('posts kirocrew-dashboard:act from a Needs-you option and nothing else', () => {
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const ask = { text: 'Ship it?', ask: 'approve', workstream: 'ship the report' }
    mount({ items: [board([task()])], omitted: 0, series: [], last_entry_at: iso(1), for_you: [ask] })
    const go = all('.pc-opt').find((b) => b.textContent === 'Go ahead') as HTMLButtonElement
    go.click()
    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0][0]).toEqual({ type: 'kirocrew-dashboard:act', text: '"Ship it?": Go ahead' })
  })

  describe('the reader locale', () => {
    const fields = () => ({ items: [board([task({ title: 'the fold' })])], omitted: 0, series: [], last_entry_at: iso(1) })

    it('draws its own words in Chinese under zh-CN and what the record wrote as written', () => {
      mount(fields(), { locale: 'zh-CN' })
      // In flight, and the tree's tag for a task.
      expect(text('.pc-kpi-l')).toBe('\u8fdb\u884c\u4e2d')
      expect(text('.pc-n-task .pc-lv')).toBe('\u4efb\u52a1')
      expect(all('.pc-n-t').map((n) => n.textContent)).toContain('the fold')
      expect(text('#pc-sentence')).toMatch(CJK)
    })

    it('redraws its own words when a push carries a new locale', () => {
      mount(fields())
      expect(text('#pc-eyebrow')).toBe('What this crewmate is doing')
      push(fields(), { locale: 'zh-CN' })
      expect(text('#pc-eyebrow')).toMatch(CJK)
      expect(text('.pc-kpi-l')).toBe('\u8fdb\u884c\u4e2d')
    })

    it('stays English under every other locale', () => {
      for (const locale of ['en', 'de', 'xx-YY']) {
        mount(fields(), { locale })
        expect(document.body.textContent).not.toMatch(CJK)
        expect(text('.pc-kpi-l')).toBe('In flight')
      }
    })

    it('lets the crewmate\u2019s own desk words win in every locale', () => {
      const ask = { text: 'Ship it?', ask: 'approve', workstream: 'ship the report' }
      const verdict = { state: 'on_track', headline: 'moving', blocker: '', words: { go: 'Ship now' } }
      mount({ ...fields(), for_you: [ask], verdict }, { locale: 'zh-CN', written_at: { verdict: iso(0) } })
      const labels = all('.pc-opt').map((b) => b.textContent)
      expect(labels).toContain('Ship now')
      // Not now, from this page's own zh-CN table, where the crewmate wrote no word.
      expect(labels).toContain('\u6682\u4e0d')
    })
  })

  it('redraws on the host push and keeps the reader in the view they chose', () => {
    const first = [board([task({ status: 'done' })], { id: 'b1' }), board([task({ item_id: 'x' })], { id: 'b2', goal: 'other' })]
    mount({ items: first, omitted: 0, series: [], last_entry_at: iso(1) })
    ;(all('.pc-pill').find((p) => p.textContent?.startsWith('ship the report')) as HTMLButtonElement).click()
    expect(text('#pc-title')).toBe('ship the report')
    const moved = [board([task({ status: 'done', state: 'accepted', verdict: 'pass' })], { id: 'b1', accepted: 1, open: 0 }), first[1]]
    push({ items: moved, omitted: 0, series: [], last_entry_at: iso(0) })
    expect(text('#pc-title')).toBe('ship the report')
    expect(text('.pc-pipe-h')).toContain('1 / 1 accepted')
  })

  it('keeps keyboard focus in an open task drawer across a host push', () => {
    const items = [board([task({ title: 'the fold' })])]
    mount({ items, omitted: 0, series: [], last_entry_at: iso(1) })
    ;(all('.pc-n-task > .pc-n-h')[0] as HTMLElement).click()
    expect(document.activeElement).toBe(document.querySelector('#pc-drawer-host .pc-dr-x'))
    push({ items, omitted: 0, series: [], last_entry_at: iso(0) })
    expect(text('.pc-dr-t')).toBe('the fold')
    expect(document.getElementById('pc-drawer-host')?.contains(document.activeElement)).toBe(true)
  })

  describe('one picked workstream', () => {
    /** A workstream whose task has its own worker's board under it, and one more workstream. */
    function nested(over: Record<string, unknown> = {}) {
      const parent = board([task({ item_id: 'it_p', title: 'parent task', spender: 'w1', credits: 4, ...over })],
        { id: 'b1', goal: 'ship the report', credits: 4 })
      const child = board([task({ item_id: 'it_c', title: 'nested blocked', status: 'blocked', spender: 'w9', credits: 3 })],
        { id: 'b1n', goal: 'the board under it', parent: { board: 'b1', item_id: 'it_p' }, credits: 3, needs_you: 1 })
      const other = board([task({ item_id: 'it_o', title: 'elsewhere', spender: 'w5' })], { id: 'b2', goal: 'other' })
      return [parent, child, other]
    }
    const pick = (goal: string) =>
      (all('.pc-pill').find((p) => p.firstChild?.textContent === goal) as HTMLButtonElement).click()
    const kpi = (label: string) =>
      all('.pc-kpi').find((k) => k.querySelector('.pc-kpi-l')?.textContent === label)?.querySelector('.pc-kpi-v')
        ?.firstChild?.textContent

    it('counts open tasks on every pill, so the pills add up to All and All to In flight', () => {
      mount({ items: nested(), omitted: 0, series: [], last_entry_at: iso(1) })
      const pills = all('.pc-pill').map((p) => [p.firstChild?.textContent, p.querySelector('.pc-pill-n')?.textContent])
      expect(pills).toEqual([['All', '3'], ['ship the report', '2'], ['other', '1']])
      expect((all('.pc-pill')[0] as HTMLElement).title).toBe('3 open tasks')
      expect(kpi('In flight')).toBe('3')
    })

    it('puts a blocked task on a board under it on its desk, as its header and counts do', () => {
      mount({ items: nested(), omitted: 0, series: [], last_entry_at: iso(1) })
      pick('ship the report')
      expect(all('.pc-card-t').map((n) => n.textContent)).toContain('nested blocked')
      expect(text('#pc-sentence')).toContain('1 needs you')
      expect(kpi('Needs you')).toBe('1')
      expect(text('.pc-crew')).toContain('worker w9')
    })

    it('states one spend in its header and its Spend card', () => {
      mount({ items: nested(), omitted: 0, series: [], last_entry_at: iso(1) })
      pick('ship the report')
      // 4 on its own board and 3 on the board under its task.
      expect(text('#pc-sentence')).toBe('accepted 0 / 2, spent 7.00 credits, 1 needs you')
      expect(text('.pc-cost-l b')).toBe('7.00')
    })

    it('shows only its own record: no crewmate verdict, and no other workstream\u2019s lane board', () => {
      const ci = { pr: '412', head: 'abc1234', lanes: [{ name: 'Unit', state: 'fail', owner: 'author', why: 'red' }] }
      const verdict = { state: 'on_track', headline: 'moving', blocker: '' }
      mount({ items: nested(), omitted: 0, series: [], last_entry_at: iso(1), verdict, ci }, { written_at: { verdict: iso(0) } })
      expect(all('.pc-verdict')).toHaveLength(1)
      expect(all('.pc-lanes')).toHaveLength(1)
      pick('ship the report')
      expect(all('.pc-verdict')).toHaveLength(0)
      expect(all('.pc-lanes')).toHaveLength(0)
      ;(all('.pc-pill')[0] as HTMLButtonElement).click()
      expect(all('.pc-verdict')).toHaveLength(1)
      expect(all('.pc-lanes')).toHaveLength(1)
    })

    it('keeps the lane board under the workstream whose task carries its pull request', () => {
      const ci = { pr: '412', head: 'abc1234', lanes: [{ name: 'Unit', state: 'fail', owner: 'author', why: 'red' }] }
      mount({ items: nested({ pr: '#412' }), omitted: 0, series: [], last_entry_at: iso(1), ci })
      pick('ship the report')
      expect(text('.pc-lanes-h')).toContain('PR #412')
      pick('other')
      expect(all('.pc-lanes')).toHaveLength(0)
    })
  })

  describe('labels that say what they count', () => {
    it('says what the Accepted total counts', () => {
      const tasks = [task({ item_id: 'a', state: 'accepted', verdict: 'pass', status: 'done' }), task({ item_id: 'b', spender: 'w2' })]
      mount({ items: [board(tasks, { total: 2, accepted: 1, open: 1 })], omitted: 0, series: [], last_entry_at: iso(1) })
      const card = all('.pc-kpi').find((k) => k.querySelector('.pc-kpi-l')?.textContent === 'Accepted')
      expect(card?.querySelector('.pc-kpi-s')?.textContent).toBe('1 of 2 tasks accepted')
    })

    it('splits the Needs-you total on the desk into workers and the crewmate', () => {
      const asks = task({ item_id: 'q', title: 'which one?', status: 'question' })
      const ask = { text: 'Ship it?', ask: 'approve', workstream: 'ship the report' }
      mount({ items: [board([asks], { needs_you: 1 })], omitted: 0, series: [], last_entry_at: iso(1), for_you: [ask] })
      const desk = all('.pc-panel').find((p) => p.querySelector('.pc-pt')?.textContent === 'Needs you')
      expect(desk?.querySelector('.pc-pc')?.textContent).toBe('2 in all: 1 from workers, 1 raised by the crewmate')
    })

    it('names states, not actions, on chips that are not buttons', () => {
      const ci = { pr: '412', head: 'abc1234', lanes: [{ name: 'Lint', state: 'did_not_run', owner: 'author', why: '' }] }
      const asks = task({ item_id: 'q', title: 'which one?', status: 'question' })
      const stuck = task({ item_id: 'b', title: 'stuck', status: 'blocked', spender: 'w2' })
      mount({ items: [board([asks, stuck])], omitted: 0, series: [], last_entry_at: iso(1), ci })
      expect(text('.pc-lane .pc-lane-o')).toBe('needs a rerun')
      const pills = all('.pc-vp').map((p) => p.textContent)
      expect(pills).toContain('Asks you')
      expect(pills).toContain('Blocked')
      for (const word of ['rerun it', 'Answer', 'Unblock']) expect(pills).not.toContain(word)
    })

    it('gives each task state one name everywhere on the page', () => {
      const asks = task({ item_id: 'q', title: 'which one?', status: 'question' })
      const stuck = task({ item_id: 'b', title: 'stuck', status: 'blocked', spender: 'w2' })
      mount({ items: [board([asks, stuck])], omitted: 0, series: [], last_entry_at: iso(1) })
      const page = document.body.textContent ?? ''
      expect(page).toContain('Asks you')
      expect(page).toContain('Blocked')
      for (const other of ['Needs an answer', 'Needs unblocking']) expect(page).not.toContain(other)
    })

    it('counts tasks in In flight and Accepted, and items only in Needs you', () => {
      const tasks = [task({ item_id: 'a', state: 'accepted', verdict: 'pass', status: 'done' }), task({ item_id: 'b', spender: 'w2' })]
      mount({ items: [board(tasks, { total: 2, accepted: 1, open: 1 })], omitted: 0, series: [], last_entry_at: iso(1) })
      const unitOf = (label: string) =>
        all('.pc-kpi').find((k) => k.querySelector('.pc-kpi-l')?.textContent === label)?.querySelector('.pc-kpi-v small')?.textContent
      expect(unitOf('In flight')).toBe('task')
      expect(unitOf('Accepted')).toBe('task')
      expect(unitOf('Needs you')).toBe('items')
    })

    it('says who fixes a failing lane in words, not a button-like tag', () => {
      const ci = { pr: '412', head: 'abc1234', lanes: [{ name: 'Unit', state: 'fail', owner: 'author', why: 'red' }] }
      mount({ items: [board([task()])], omitted: 0, series: [], last_entry_at: iso(1), ci })
      expect(text('.pc-lane .pc-lane-o')).toBe('yours to fix')
      expect(all('.pc-lane .pc-chip')).toHaveLength(0)
    })

    it('says under every card with buttons that a press only fills the chat box', () => {
      const ask = { text: 'Ship it?', ask: 'approve', workstream: 'ship the report' }
      mount({ items: [board([task()])], omitted: 0, series: [], last_entry_at: iso(1), for_you: [ask] })
      const card = all('.pc-opt').find((b) => b.textContent === 'Go ahead')?.closest('.pc-card')
      expect(card?.querySelector('.pc-opt-note')?.textContent).toContain('Nothing is sent until you press send')
    })
  })
})
