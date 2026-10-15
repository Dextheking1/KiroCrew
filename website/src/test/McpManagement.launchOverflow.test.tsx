// MCP sharing launch review: a long unbroken launch value must wrap, not clip.
//
// Contract under test (issue #20644):
// - A launch argument can be a single long token with no spaces, e.g. a
//   comma-joined list of directory paths. `overflow-wrap: break-word`
//   (`break-words`) does NOT reduce the min-content width of a flex item, so
//   such a token keeps the chip at its intrinsic width and forces the command
//   row wider than the full-width review cell, clipping the right edge with no
//   way to scroll to it. The operator is then approving a command they cannot
//   read in full.
// - The fix is the same wrap the sibling chips in this surface already use:
//   `[overflow-wrap:anywhere]`, which DOES reduce min-content width, plus
//   `min-w-0` so the flex item is allowed to shrink below its content.
// - The "Declared in the config of: ..." line is a plain paragraph whose only
//   content is the joined agent list; a long list clips for the same reason, so
//   it must carry `[overflow-wrap:anywhere]` too.
//
// jsdom does not compute layout widths, so the overflow is asserted the way this
// repo asserts wrapping elsewhere (CodeBlock.proseWrap, ToolCallLine.labelClamp):
// on the wrapping class of the rendered element.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { McpManagement } from '../pages/settings/McpManagement'
import { api } from '../api/client'

// A single argument with no spaces: a comma-joined list of many directory
// paths, exactly the shape the issue names. Nothing in it is a wrap opportunity
// for a normal word break.
const LONG_ARG =
  '--roots=/home/user/workspace/project-one,/home/user/workspace/project-two,' +
  '/home/user/workspace/project-three,/home/user/workspace/project-four,' +
  '/home/user/workspace/project-five,/home/user/workspace/project-six'

const server = {
  name: 'alpha-mcp',
  stub: true,
  can_stub: true,
  in_allowlist: true,
  entry_poolable: false,
  // A long list of configs, so the "Declared in the config of" line is long too.
  agents: ['kirocrew', 'amzn-builder', 'apollo-deployment-assistant', 'atlas', 'issue-radar'],
  transport: 'stdio',
  denylisted: false,
}

const status = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  stub: ['alpha-mcp'],
  stub_count: 1,
  running: true,
  ping_ok: true,
  supported: true,
  ...over,
})

function mount() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpManagement />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
})
afterEach(cleanup)

describe('McpManagement launch review overflow (#20644)', () => {
  beforeEach(() => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': {
            reason: 'changed_needs_reapproval',
            commands: [['npx', '-y', 'alpha-mcp@2', LONG_ARG]],
            envs: [[]],
            expected_launch: 'a:b',
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server] } as never)
  })

  it('wraps a long unbroken launch argument instead of clipping it', async () => {
    mount()
    const command = await screen.findByRole('list', {
      name: 'Command',
    })
    const chip = within(command)
      .getAllByRole('listitem')
      .find(item => item.textContent === LONG_ARG)
    expect(chip).toBeTruthy()
    // break-words alone does not reduce a flex item's min-content width, so the
    // row stays wider than the card. The anywhere wrap does, and min-w-0 lets
    // the flex item shrink below its content.
    expect(chip!.className).toContain('[overflow-wrap:anywhere]')
    expect(chip!.className).toContain('min-w-0')
  })

  it('wraps the Declared-in-the-config line when the list is long', async () => {
    mount()
    const declared = await screen.findByText(/Declared in the config of:/)
    expect(declared.className).toContain('[overflow-wrap:anywhere]')
  })
})
