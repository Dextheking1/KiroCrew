/**
 * `lib/themeCssImport` — the Paste CSS tab's parser. Pins which mode each
 * selector form lands in, what is dropped and reported, and that the
 * serialiser's output parses back to the same variables.
 */
import { describe, it, expect } from 'vitest'

import { applyThemeCss, parseThemeCss, themeVarsToCss } from '../lib/themeCssImport'

describe('parseThemeCss', () => {
  it.each([
    ['.dark { --bg: #111111; }', 'dark'],
    ['[data-theme="dark"] { --bg: #111111; }', 'dark'],
    ['html.theme-dark { --bg: #111111; }', 'dark'],
    ['@media (prefers-color-scheme: dark) { :root { --bg: #111111; } }', 'dark'],
    ['.light { --bg: #111111; }', 'light'],
    [':root { --bg: #111111; }', 'shared'],
    ['--bg: #111111;', 'shared'],
    ['.dark, .light { --bg: #111111; }', 'shared'],
    ['.darkness { --bg: #111111; }', 'shared'],
  ] as const)('reads %s as %s', (css, mode) => {
    const r = parseThemeCss(css)
    expect(r[mode]).toEqual({ '--bg': '#111111' })
    expect(r.recognized).toBe(1)
  })

  it('returns to the outer context after a nested block closes', () => {
    const r = parseThemeCss('@media (prefers-color-scheme: dark) { :root { --bg: #000; } } :root { --text: #fff; }')
    expect(r.dark).toEqual({ '--bg': '#000' })
    expect(r.shared).toEqual({ '--text': '#fff' })
  })

  it('strips comments, !important and line-wrapped whitespace', () => {
    const r = parseThemeCss(':root {\n  /* brand */ --accent: #7c3aed !important;\n  --shadow-sm: 0 1px 2px\n    rgba(0, 0, 0, .2);\n}')
    expect(r.shared).toEqual({ '--accent': '#7c3aed', '--shadow-sm': '0 1px 2px rgba(0, 0, 0, .2)' })
  })

  it('reports unknown properties and refused values without applying them', () => {
    const r = parseThemeCss(':root { --brand: red; color: blue; --bg: url(evil); --text: #000;}html{filter:invert(1)}')
    expect(r.unknown).toEqual(['--brand', 'color', 'filter'])
    expect(r.invalid).toEqual(['--bg'])
    expect(r.shared).toEqual({ '--text': '#000' })
  })

  it('drops an invalid report once the same mode redeclares the variable validly', () => {
    expect(parseThemeCss(':root { --bg: "x"; --bg: #000; }').invalid).toEqual([])
    expect(parseThemeCss('.dark { --bg: "x"; } .light { --bg: #000; }').invalid).toEqual(['--bg'])
  })

  it('treats property names case-sensitively, as CSS does', () => {
    const r = parseThemeCss('.dark { --bg: #000; --BG: #fff; }')
    expect(r.dark).toEqual({ '--bg': '#000' })
    expect(r.unknown).toEqual(['--BG'])
  })

  it('finds nothing in empty or variable-free input', () => {
    expect(parseThemeCss('').recognized).toBe(0)
    expect(parseThemeCss('body { color: red }').recognized).toBe(0)
  })
})

describe('applyThemeCss', () => {
  it('lays shared then mode-specific values over the existing maps', () => {
    const imported = parseThemeCss(':root { --bg: #222; --accent: #f00; } .dark { --bg: #000; }')
    const out = applyThemeCss(imported, { '--bg': '#999', '--text': '#eee' }, { '--bg': '#fff', '--text': '#111' })
    expect(out.dark).toEqual({ '--bg': '#000', '--text': '#eee', '--accent': '#f00' })
    expect(out.light).toEqual({ '--bg': '#222', '--text': '#111', '--accent': '#f00' })
  })
})

describe('themeVarsToCss', () => {
  it('round-trips through the parser and skips empty values', () => {
    const dark = { '--bg': '#000000', '--text': '#ffffff', '--ring': '' }
    const light = { '--bg': '#ffffff', '--text': '#000000' }
    const r = parseThemeCss(themeVarsToCss(dark, light))
    expect(r.dark).toEqual({ '--bg': '#000000', '--text': '#ffffff' })
    expect(r.light).toEqual(light)
    expect(r.shared).toEqual({})
  })

  it('is empty when there is nothing to serialise', () => {
    expect(themeVarsToCss({}, {})).toBe('')
  })
})
