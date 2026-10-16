/**
 * Settings ▸ Slack ▸ Slack Code: the agent-sessions and code-channels switches,
 * the code-channel defaults, and the "Update your Slack app" panel.
 *
 * Seams: the Slack config/manifest/status calls and the folder and agent lists
 * are spied on the real `api` object; the global fetch stub answers the schema
 * read the restart badges make.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import { api, type SlackConfigData, type SlackManifestStatus } from '../api/client'
import { SlackPanel } from '../pages/settings/SlackPanel'
import { slackCodePayload, slackCodeDraftFrom } from '../pages/settings/SlackCodePanel'
import { markModelsDegraded } from '../providers/modelListHealth'

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ entries: [
      { path: 'slack.code_channels', type: 'boolean', requiresRestart: true },
    ] }),
  })) as unknown as typeof fetch)
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const FOLDERS = [
  { id: 'aaaaaaaaaaaa', name: 'Dev', order: 0, project_dir: '/repo/app' },
  { id: 'bbbbbbbbbbbb', name: 'Slack Code', order: 1, parent_id: 'aaaaaaaaaaaa', default_agent: 'builder' },
]

const NO_CHANGE = { added_scopes: [], removed_scopes: [], added_events: [], removed_events: [], added_features: [], removed_features: [] }
const CHANGES = {
  code_channels: { ...NO_CHANGE, added_features: ['code_channels'], added_scopes: ['canvases:write', 'chat:write.customize', 'code_channels:manage'], added_events: ['agent_session_stopped'] },
}

function status(install: Partial<SlackManifestStatus['install']> = {}, over: Partial<SlackManifestStatus> = {}): SlackManifestStatus {
  return {
    features: ['code_channels'],
    required_scopes: [],
    required_events: [],
    required_features: [],
    feature_changes: CHANGES,
    install: { status: 'up_to_date', missing_scopes: [], extra_scopes: [], disabled_features: [], app_id: 'A0123ABCD', team_id: 'T1', reason: '', ...install },
    app_manifest_url: 'https://api.slack.com/apps/A0123ABCD/app-manifest',
    ...over,
  }
}

function config(over: Partial<SlackConfigData> = {}): SlackConfigData {
  return {
    connected: true, connect_error: '', configured: true, read_only: false,
    bot_token_set: true, app_token_set: true, bot_token_preview: 'xoxb-••••', app_token_preview: 'xapp-••••',
    owner_id: 'U000OWNER', command: 'kirocrew', allowed_enterprise_ids: [],
    reactions_enabled: true, show_thinking: true,
    code_channels: false,
    code_channel_defaults: { folder: null, agent: null, model: null, reasoning_effort: null },
    code_channel_effective: {
      folder: null, agent: 'kirocrew', agent_source: 'slack', model: null, reasoning_effort: null,
      project_dir: null, project_source: '', slack_agent: 'kirocrew',
    },
    code_channel_warnings: [],
    ...over,
  }
}

function seed(cfg: Partial<SlackConfigData> = {}, st: SlackManifestStatus = status()) {
  vi.spyOn(api, 'getSlackConfig').mockResolvedValue(config(cfg))
  vi.spyOn(api, 'getSlackManifest').mockResolvedValue({ alias: 'kirocrew', manifest: 'display_information: {}\n', create_url: 'https://example.invalid/new' })
  const getStatus = vi.spyOn(api, 'getSlackManifestStatus').mockResolvedValue(st)
  vi.spyOn(api, 'chatFolders').mockResolvedValue(FOLDERS as never)
  vi.spyOn(api, 'agentsInstalled').mockResolvedValue([{ name: 'builder' }, { name: 'reviewer' }])
  const save = vi.spyOn(api, 'saveSlackConfig').mockResolvedValue({ ok: true, restart_required: true, verify_warning: '' })
  renderWithProviders(<SlackPanel />)
  return { save, getStatus }
}

const hydrated = () => screen.findByRole('heading', { name: 'Slack', level: 3 })
const saveBtn = () => screen.getByRole('button', { name: 'Save Slack settings' })
/** The manifest panel and the capability lines appear only after a chain of
 *  queries: the Slack config, the effect that seeds the draft from it, then the
 *  manifest-status query. A loaded coverage runner can take longer than the
 *  implicit one-second find deadline over that chain, so the waits on it name
 *  their own. */
const CHAINED = { timeout: 5000 }

describe('Slack Code payload', () => {
  it('sends only what changed, so an unrelated save never re-validates a default', () => {
    const saved = slackCodeDraftFrom(config({ code_channel_defaults: { folder: 'aaaaaaaaaaaa', agent: null, model: 'gone-model', reasoning_effort: null } }))
    expect(slackCodePayload(saved, saved)).toEqual({})
    expect(slackCodePayload({ ...saved, effort: 'high' }, saved)).toEqual({ code_channel_defaults: { reasoning_effort: 'high' } })
    expect(slackCodePayload({ ...saved, folder: '', switches: { code_channels: true } }, saved)).toEqual({
      code_channels: true,
      code_channel_defaults: { folder: null },
    })
  })

  it('stores the inherit model as unset', () => {
    const saved = slackCodeDraftFrom(config({ code_channel_defaults: { folder: null, agent: null, model: 'claude-x', reasoning_effort: null } }))
    expect(slackCodePayload({ ...saved, model: 'auto' }, saved)).toEqual({ code_channel_defaults: { model: null } })
  })
})

describe('Code channels card', () => {
  it('reveals the defaults, previews the folder resolution and saves the picks', async () => {
    const { save } = seed()
    await hydrated()
    expect(screen.queryByTestId('slack-code-defaults')).toBeNull()

    fireEvent.click(screen.getByRole('switch', { name: 'Code channels' }))
    const card = await screen.findByTestId('slack-code-defaults', {}, CHAINED)
    await userEvent.click(within(card).getByLabelText('Default folder'))
    await userEvent.click(await screen.findByRole('option', { name: /Slack Code/ }))

    const preview = screen.getByTestId('slack-code-preview')
    expect(preview).toHaveTextContent('/repo/app')   // inherited from the parent folder
    expect(preview).toHaveTextContent('builder')     // the folder's default agent
    expect(preview).toHaveTextContent('Folder default')

    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({
      code_channels: true,
      code_channel_defaults: { folder: 'bbbbbbbbbbbb' },
    })
    expect(save.mock.calls[0][0]).not.toHaveProperty('agent_sessions')
  })

  it('explains a saved folder that was deleted since', async () => {
    seed({
      code_channels: true,
      code_channel_defaults: { folder: '999999999999', agent: null, model: null, reasoning_effort: null },
      code_channel_warnings: ['folder_missing'],
    })
    expect(await screen.findByText(/The saved folder no longer exists/)).toBeInTheDocument()
  })

  it('says when the model list could not load', async () => {
    markModelsDegraded('acp', true)
    try {
      seed({ code_channels: true })
      expect(await screen.findByText(/Could not load the model list/, {}, CHAINED)).toBeInTheDocument()
    } finally {
      markModelsDegraded('acp', false)
    }
  })

  it('previews a folder the server reports missing as unfiled, even if the list still has it', async () => {
    seed({
      code_channels: true,
      code_channel_defaults: { folder: 'bbbbbbbbbbbb', agent: null, model: null, reasoning_effort: null },
      code_channel_warnings: ['folder_missing'],
    })
    const preview = await screen.findByTestId('slack-code-preview', {}, CHAINED)
    await waitFor(() => expect(preview).toHaveTextContent('Unfiled'))
    expect(preview).not.toHaveTextContent('Slack Code')
    expect(preview).not.toHaveTextContent('/repo/app')
    expect(preview).not.toHaveTextContent('builder')
    // The picker does not keep showing the folder the notice says is gone.
    expect(within(screen.getByTestId('slack-code-defaults')).getByLabelText('Default folder')).toHaveTextContent('No folder')
  })

  it('explains a saved agent that was removed since', async () => {
    seed({
      code_channels: true,
      code_channel_defaults: { folder: null, agent: 'ghost', model: null, reasoning_effort: null },
      code_channel_warnings: ['agent_missing'],
    })
    expect(await screen.findByText(/“ghost” is no longer installed/)).toBeInTheDocument()
  })
})

describe('Update your Slack app', () => {
  it('stays hidden while no switch is on and nothing is left over', async () => {
    seed({}, status({}, { features: [] }))
    await hydrated()
    await waitFor(() => expect(api.getSlackManifestStatus).toHaveBeenCalled())
    expect(screen.queryByTestId('slack-manifest-update')).toBeNull()
  })

  it('lists what code channels add, marks the missing scopes and gives the steps', async () => {
    seed({ code_channels: true }, status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'needs_update')
    expect(panel).toHaveTextContent('features.code_channels')
    expect(panel).toHaveTextContent('code_channels:manage· missing')
    expect(panel).toHaveTextContent('reinstall the app')
    expect(within(panel).getByTestId('slack-manifest-replace-warning')).toHaveTextContent('whole configuration')
    expect(within(panel).getByRole('link', { name: /Open App Manifest/ }))
      .toHaveAttribute('href', 'https://api.slack.com/apps/A0123ABCD/app-manifest')
  })

  it('reads Up to date after a reinstall, via Check again', async () => {
    const { getStatus } = seed({ code_channels: true }, status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    getStatus.mockResolvedValue(status())
    const cfgCalls = () => vi.mocked(api.getSlackConfig).mock.calls.length
    const cfgBefore = cfgCalls()
    fireEvent.click(within(panel).getByRole('button', { name: /Check again/ }))
    await waitFor(() => expect(getStatus).toHaveBeenLastCalledWith(true))
    await waitFor(() => expect(within(panel).getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'up_to_date'))
    expect(panel).toHaveTextContent('Nothing to update.')
    // Lines the install holds no longer read as additions.
    expect(panel).not.toHaveTextContent('+ code_channels:manage')
    expect(panel).toHaveTextContent('code_channels:manage')
    expect(screen.queryByTestId('slack-manifest-replace-warning')).toBeNull()
    // The capability lines (config query) are re-read after the check.
    await waitFor(() => expect(cfgCalls()).toBeGreaterThan(cfgBefore))
  })

  it('names a workspace that does not have code channels enabled', async () => {
    seed({ code_channels: true }, status({ status: 'feature_disabled', disabled_features: ['code_channels'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Workspace not enabled')
    expect(panel).toHaveTextContent('does not have Code channels enabled')
    expect(panel).toHaveTextContent('No app update is needed')
    // Reinstalling does not enable a workspace feature, so no update steps.
    expect(panel).not.toHaveTextContent('reinstall the app')
  })

  it('shows a rejected bot token as an error, with no update steps', async () => {
    seed({ code_channels: true }, status({ status: 'token_rejected', reason: 'invalid_auth' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Bot token rejected')
    expect(within(panel).getByText(/Slack rejected the saved bot token/)).toBeInTheDocument()
    expect(panel).not.toHaveTextContent('reinstall the app')
  })

  it('says when Slack would not report the scopes', async () => {
    seed({ code_channels: true }, status({ status: 'unknown', reason: 'scopes_unreadable' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByTestId('slack-manifest-status')).toHaveTextContent('Cannot check')
    // A scope Slack did not report is neither held nor to be added.
    expect(panel).not.toHaveTextContent('+ code_channels:manage')
    expect(panel).toHaveTextContent('code_channels:manage')
    expect(within(panel).queryByText(/could not be reached/)).toBeNull()
  })

  it('shows a Slack call that failed as an error', async () => {
    seed({ code_channels: true }, status({ status: 'unknown', reason: 'ConnectionError' }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(within(panel).getByText(/Slack could not be reached to check the app/)).toBeInTheDocument()
    expect(panel).not.toHaveTextContent('did not report which permissions')
  })

  it('puts the hand-edited-app exception before the update steps', async () => {
    seed({ code_channels: true }, status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] }))
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    const warning = within(panel).getByTestId('slack-manifest-replace-warning')
    const steps = within(panel).getByRole('list')
    expect(warning.compareDocumentPosition(steps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('previews an unsaved switch as pending', async () => {
    seed({}, status({}, { features: [] }))
    await hydrated()
    fireEvent.click(screen.getByRole('switch', { name: 'Code channels' }))
    expect(await screen.findByTestId('slack-manifest-pending', {}, CHAINED)).toBeInTheDocument()
    expect(screen.getByTestId('slack-manifest-update')).toHaveTextContent('after you save')
    expect(screen.getByTestId('slack-manifest-pending')).toHaveTextContent('Save Slack settings')
    // The install reading is about the saved manifest: the pill does not
    // call the on-screen one up to date.
    expect(screen.getByTestId('slack-manifest-status')).toHaveAttribute('data-status', 'pending')
    // The update steps wait for the save: the reading is about the saved
    // manifest, so neither its detail line nor the steps show yet.
    expect(screen.getByTestId('slack-manifest-update')).not.toHaveTextContent('reinstall the app')
    expect(screen.getByTestId('slack-manifest-update')).not.toHaveTextContent('has every permission')
  })
})

describe('Slack Code switch registry', () => {
  it('offers only the registered switches', async () => {
    seed()
    await hydrated()
    expect(screen.getByRole('switch', { name: 'Code channels' })).toBeInTheDocument()
    // Agent sessions lands with its runtime: this build offers no switch for it.
    expect(screen.queryByRole('switch', { name: 'Agent sessions' })).toBeNull()
  })

  it('names a feature the server reports that this build does not register by its raw name', async () => {
    seed(
      { code_channels: true },
      status({ status: 'feature_disabled', disabled_features: ['code_channels', 'future_switch'] }),
    )
    const panel = await screen.findByTestId('slack-manifest-update', {}, CHAINED)
    expect(panel).toHaveTextContent('Code channels, future_switch')
  })
})

describe('Capability line with a rejected token', () => {
  it('says the token, not the scopes, is what to fix', async () => {
    seed({
      code_channels: true,
      code_channels_capability: { available: false, reason: 'scopes_unknown', scope: 'code_channels:manage' },
    }, status({ status: 'token_rejected', reason: 'invalid_auth' }))
    await waitFor(() => expect(screen.getByTestId('slack-capability')).toHaveTextContent('Needs a new bot token'), CHAINED)
  })
})

describe('Capability line under a saved toggle', () => {
  it('says the toggle is not running yet while the scope is missing', async () => {
    seed({
      code_channels: true,
      code_channels_capability: { available: false, reason: 'missing_scope', scope: 'code_channels:manage' },
    }, status({ status: 'needs_update', missing_scopes: ['code_channels:manage'] }))
    const line = await screen.findByTestId('slack-capability', {}, CHAINED)
    expect(line).toHaveAttribute('data-available', 'false')
    expect(line).toHaveTextContent('code_channels:manage')
    expect(line).toHaveTextContent('Slack works as before')
  })

  it('says a workspace without the feature keeps working as before', async () => {
    seed({
      code_channels: true,
      code_channels_capability: { available: false, reason: 'feature_disabled', scope: 'code_channels:manage' },
    })
    const line = await screen.findByTestId('slack-capability', {}, CHAINED)
    expect(line).toHaveAttribute('data-reason', 'feature_disabled')
    expect(line).toHaveTextContent('Needs a workspace admin')
  })

  it('reads Ready once the app holds the permissions', async () => {
    seed({
      code_channels: true,
      code_channels_capability: { available: true, reason: '', scope: 'code_channels:manage' },
    })
    expect(await screen.findByTestId('slack-capability', {}, CHAINED)).toHaveTextContent('Ready')
  })

  it('shows nothing under a toggle that is off', async () => {
    seed({ code_channels_capability: { available: false, reason: 'scopes_unknown', scope: 'code_channels:manage' } })
    await hydrated()
    expect(screen.queryByTestId('slack-capability')).toBeNull()
  })
})
