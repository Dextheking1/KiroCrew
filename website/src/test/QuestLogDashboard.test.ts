import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'

type Item = Record<string, unknown>
type DashboardApi = {
  fields: Record<string, unknown>
  agentic: string[]
  written_at: Record<string, string>
  locale: string
  seq: number
  stale: boolean
}

type DashboardWindow = Window & { kirocrew: DashboardApi }

const root = resolve(__dirname, '../../..')
const html = readFileSync(resolve(root, 'src/kiro_crew/dashboard_templates/builtin/quest-log/template.html'), 'utf8')
const sample = JSON.parse(readFileSync(resolve(root, 'test/fixtures/dashboard_templates/sample_quest-log.json'), 'utf8')) as {
  agentic: { advice: unknown[] }
  agentic_written_at: Record<string, string>
  folds: { work: { seq: number; value: { conductor: Record<string, unknown>; items: Item[]; omitted: number; forgotten: Record<string, number> } } }
}

function templateParts(): { scripts: string[]; body: HTMLElement } {
  const parsed = new DOMParser().parseFromString(html, 'text/html')
  const nodes = Array.from(parsed.querySelectorAll('script'))
  const scripts = nodes.map(node => node.textContent ?? '')
  for (const node of nodes) node.remove()
  return { scripts, body: parsed.body }
}

function sampleFields(): Record<string, unknown> {
  const work = structuredClone(sample.folds.work.value)
  return {
    goal: work.conductor.goal,
    round: work.conductor.round,
    last_entry_at: work.conductor.last_entry_at,
    items: work.items,
    omitted: work.omitted,
    forgotten: work.forgotten,
    advice: structuredClone(sample.agentic.advice),
  }
}

function bind(fields: Record<string, unknown>): void {
  document.querySelectorAll<HTMLElement>('[data-dashboard-field]').forEach((node) => {
    const name = node.dataset.dashboardField ?? ''
    const value = fields[name]
    node.textContent = value === undefined || value === null
      ? ''
      : typeof value === 'object' ? JSON.stringify(value) : String(value)
  })
}

function render(fields = sampleFields(), writtenAt = sample.agentic_written_at, agentic = ['advice']): void {
  const { scripts, body } = templateParts()
  document.body.replaceChildren(...Array.from(body.childNodes))
  bind(fields)
  ;(window as DashboardWindow).kirocrew = {
    fields,
    agentic,
    written_at: writtenAt,
    locale: 'en',
    seq: sample.folds.work.seq,
    stale: false,
  }
  for (const source of scripts) new Function(source)()
}

beforeEach(() => {
  document.body.replaceChildren()
})

describe('quest-log dashboard', () => {
  it('draws every section from its declared source', () => {
    render()

    expect(document.querySelector('.ql-goal')?.textContent).toBe('Ship the new right-panel task view')
    expect(document.querySelector('#ql-state')?.textContent).toBe('2 active · 1 needs you · 1 upcoming · 7 finished')
    expect(document.querySelectorAll('#ql-needs .ql-row')).toHaveLength(1)
    expect(document.querySelector('#ql-needs')?.textContent).toContain('Answer this in chat: Choose the release lane.')
    expect(document.querySelectorAll('#ql-active .ql-row')).toHaveLength(2)
    expect(document.querySelector('#ql-advice')?.textContent).toContain('Review the release choice before more work starts.')
    expect(document.querySelector('#ql-advice')?.textContent).toContain('Pick the release lane')
    expect(document.querySelector('#ql-advice')?.textContent).not.toContain('it_needs')
    expect(document.querySelectorAll('#ql-upcoming .ql-row')).toHaveLength(1)
    expect(document.querySelector('#ql-upcoming')?.textContent).toContain('Write the rollout note')
    expect(document.querySelector('#ql-finished')?.textContent).toBe('7')
    expect(document.querySelector('#ql-omitted')?.textContent).toBe('3 log entries omitted')
  })

  it('keeps every section useful when no data or advice exists', () => {
    render({
      goal: '',
      round: 0,
      last_entry_at: '',
      items: [],
      omitted: 0,
      forgotten: {},
      advice: [],
    }, {}, [])

    expect(document.querySelector('.ql-goal')?.textContent).toBe('No current goal in the work log.')
    expect(document.querySelector('#ql-state')?.textContent).toBe('0 active · 0 needs you · 0 upcoming · 0 finished')
    expect(document.querySelector('#ql-needs')?.textContent).toContain('Nothing needs you right now.')
    expect(document.querySelector('#ql-active')?.textContent).toContain('No quest is active right now.')
    expect(document.querySelector('#ql-advice')?.textContent).toContain('No current advice.')
    expect(document.querySelector('#ql-upcoming')?.textContent).toContain('No queued quest or dependency is recorded.')
    expect(document.querySelectorAll('a')).toHaveLength(0)
  })

  it('hides advice older than the work fold', () => {
    render(sampleFields(), { advice: '2026-10-14T06:20:00Z' })

    expect(document.querySelector('#ql-advice')?.textContent).toContain('Advice is older than the work log')
    expect(document.querySelector('#ql-advice')?.textContent).not.toContain('Review the release choice')
  })

  it('renders hostile task and advice text as text only', () => {
    const fields = sampleFields()
    const items = fields.items as Item[]
    items[0] = {
      ...items[0],
      title: '<img src=x onerror="window.pwned=1">Long task',
      summary: '<script>window.pwned=2</script>',
    }
    fields.advice = [{ text: '<b>Do not trust markup</b>', why: '<svg onload="window.pwned=3"></svg>' }]
    render(fields)

    expect(document.querySelector('.ql img')).toBeNull()
    expect(document.querySelector('.ql svg[onload]')).toBeNull()
    expect(document.querySelector('.ql script')).toBeNull()
    expect(document.querySelector('#ql-needs')?.textContent).toContain('<img src=x onerror=')
    expect(document.querySelector('#ql-advice')?.textContent).toContain('<b>Do not trust markup</b>')
  })

  it('caps a board with more than fifty items and names the rest', () => {
    const fields = sampleFields()
    fields.items = Array.from({ length: 55 }, (_, index) => ({
      item_id: `it_${index}`,
      title: index === 0 ? 'A'.repeat(400) : `Task ${index}`,
      state: 'open',
      status: 'progress',
      summary: '',
      acceptance: { kind: 'file', path: `task-${index}` },
      pr: null,
    }))
    fields.forgotten = {}
    fields.omitted = 0
    fields.advice = []
    render(fields, {}, [])

    expect(document.querySelector('#ql-active-count')?.textContent).toBe('55')
    expect(document.querySelectorAll('#ql-active .ql-row')).toHaveLength(12)
    expect(document.querySelector('#ql-active .ql-more')?.textContent).toBe('43 more not shown')
    expect(document.querySelector('#ql-active .ql-title')?.textContent).toBe('A'.repeat(400))
  })

  it('keeps conductor questions out of Needs you', () => {
    const fields = sampleFields()
    fields.items = [{
      item_id: 'it_conductor',
      title: 'Pick the next work item',
      state: 'open',
      status: 'question',
      reason: null,
      summary: 'The conductor must choose.',
    }]
    fields.forgotten = {}
    fields.advice = []
    render(fields, {}, [])

    expect(document.querySelector('#ql-needs-count')?.textContent).toBe('0')
    expect(document.querySelector('#ql-active-count')?.textContent).toBe('1')
    expect(document.querySelector('#ql-active .ql-tag')?.textContent).toBe('Conductor deciding')
  })

  it('uses gold rather than needs-you red for a dependency', () => {
    const fields = sampleFields()
    fields.items = [{
      item_id: 'it_dependency',
      title: 'Wait for the build',
      state: 'open',
      status: 'blocked',
      reason: null,
      summary: 'The build is still running.',
    }]
    fields.forgotten = {}
    fields.advice = []
    render(fields, {}, [])

    expect(document.querySelector('#ql-upcoming .ql-tag')?.textContent).toBe('Dependency')
    expect(document.querySelector<HTMLElement>('#ql-upcoming .ql-row')?.style.getPropertyValue('--ql-tone')).toBe('var(--ql-gold)')
  })

  it('draws map markers and party frames from real task status', () => {
    render()

    expect(document.querySelectorAll('#ql-map-pins .ql-pin')).toHaveLength(4)
    expect(document.querySelector('#ql-map-pins .ql-pin-need')?.textContent).toBe('!')
    expect(document.querySelector('#ql-map-pins .ql-pin-ready')?.textContent).toBe('?')
    expect(document.querySelectorAll('#ql-party .ql-member')).toHaveLength(3)
    expect(document.querySelector('#ql-xp-count')?.textContent).toBe('5 / 11')
  })

  it('flips a quest card by click and Enter', () => {
    render()

    const card = document.querySelector<HTMLElement>('#ql-active .ql-row')
    expect(card).not.toBeNull()
    expect(card?.classList.contains('is-flipped')).toBe(false)
    card?.click()
    expect(card?.classList.contains('is-flipped')).toBe(true)
    expect(card?.querySelector('.ql-back')?.textContent).toContain('Last report')
    card?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(card?.classList.contains('is-flipped')).toBe(false)
  })

  it('does not arm pointer parallax with reduced motion', () => {
    const original = window.matchMedia
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: () => ({ matches: true, addEventListener: () => {}, removeEventListener: () => {} }),
    })
    try {
      render()
      const table = document.querySelector<HTMLElement>('#ql-war-table')
      table?.dispatchEvent(new PointerEvent('pointerenter', { bubbles: true, clientX: 10, clientY: 10 }))
      table?.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: 200, clientY: 100 }))
      expect(table?.style.transform).toBe('')
      expect(document.querySelector('.ql')?.textContent).toContain('War table')
    } finally {
      Object.defineProperty(window, 'matchMedia', { configurable: true, value: original })
    }
  })

  it('makes each PR a safe real link', () => {
    render()

    const links = [...document.querySelectorAll<HTMLAnchorElement>('.ql-link')]
    expect(links).toHaveLength(2)
    for (const link of links) {
      expect(link.href).toMatch(/^https:\/\/github\.com\/kirodotdev\/KiroCrew\/pull\/2010[12]$/)
      expect(link.target).toBe('_blank')
      expect(link.rel).toBe('noopener noreferrer')
    }
  })
})
