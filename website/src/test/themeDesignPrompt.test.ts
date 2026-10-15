/**
 * `lib/themeDesign.prompt` — the AI theme-design prompt and the Paste CSS parser
 * must agree, or a user follows the prompt and gets a reply the tab rejects.
 */
import { describe, it, expect } from 'vitest'

import { ALLOWED_CSS_VARS } from '../hooks/themeCss'
import { parseThemeCss } from '../lib/themeCssImport'
import { THEME_PROMPT_GROUPS, themeDesignPrompt } from '../lib/themeDesign.prompt'

const promptVars = THEME_PROMPT_GROUPS.flatMap(g => g.vars.map(([name]) => name))

describe('themeDesignPrompt', () => {
  it('asks for exactly the variables a theme may set, each once', () => {
    expect(new Set(promptVars).size).toBe(promptVars.length)
    expect(new Set(promptVars)).toEqual(ALLOWED_CSS_VARS)
  })

  it('lists every variable in the prompt text', () => {
    const prompt = themeDesignPrompt()
    for (const name of promptVars) expect(prompt).toContain(`  ${name} — `)
  })

  it("shows an example reply the Paste CSS tab reads cleanly, name and emoji included", () => {
    const fence = /```css\n([\s\S]*?)\n```/.exec(themeDesignPrompt())
    expect(fence).not.toBeNull()
    const r = parseThemeCss(fence![1])
    expect(r.name).toBe('Harbor Dusk')
    expect(r.emoji).toBe('⚓')
    expect(r.unknown).toEqual([])
    expect(r.invalid).toEqual([])
    expect(r.dark['--accent']).toBe('#4fb3c8')
    expect(r.light['--accent']).toBe('#1f7f94')
    expect(r.shared).toEqual({})
  })

  it('reads only the code block of a whole chat reply pasted at once', () => {
    const r = parseThemeCss(
      'Here is a dark and light theme for you:\n\n```css\n/* name: Moonlight */\n.light { --bg: #fff; }\n```\n\nEnjoy!',
    )
    expect(r.name).toBe('Moonlight')
    // Without isolating the fence, the prose above joins the first selector and
    // its "dark and light" makes the block read as shared.
    expect(r.light).toEqual({ '--bg': '#fff' })
    expect(r.shared).toEqual({})
  })
})
