/**
 * Feature previews are kept by the gateway (`dashboard.feature_previews.<name>`)
 * with localStorage as a cache. These tests pin the four parts of that contract:
 * the server copy fills an empty cache, a toggle is written to the server, a
 * failed config read leaves the cache in charge, and a switch set before the
 * server kept it is carried up exactly once without replacing a stored value.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook, act, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import {
  PREVIEW_FLAGS_SYNCED_KEY, __resetPreviewFlagServerSyncForTests, clearPreviewFlagWriteError,
  usePreviewFlagServerSync, usePreviewFlagWriteError,
} from '../hooks/usePreviewFlagServerSync'
import * as previewFlags from '../utils/previewFlags'
import {
  PREVIEW_ARTIFACT_DEPLOY, PREVIEW_CREW, PREVIEW_FLAGS, PREVIEW_FLAG_PREFIX, PREVIEW_INSTANCE_SESSIONS,
  PREVIEW_REMOTE_CREW_CHAT, PREVIEW_WEBHOOKS, previewFlagConfigPath, readPreviewFlag, setPreviewFlag,
  setRemoteCrewChat,
} from '../utils/previewFlags'

type Body = { dashboard?: { feature_previews?: unknown } }

function harness(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

function newClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
}

/** Mount the sync next to a reader of the Crewmates flag, as the app shell does. */
function mountSync(qc: QueryClient) {
  return renderHook(() => {
    usePreviewFlagServerSync()
    return usePreviewFlag(PREVIEW_CREW)
  }, { wrapper: harness(qc) })
}

let server: Record<string, boolean>
let patchSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  localStorage.clear()
  __resetPreviewFlagServerSyncForTests()
  server = {}
  vi.spyOn(api, 'kirocrewConfig').mockImplementation(
    async () => ({ dashboard: { feature_previews: { ...server } } }) as Body,
  )
  patchSpy = vi.spyOn(api, 'patchConfig').mockImplementation(async (path: string, value: unknown, opts?: { ifUnset?: boolean }) => {
    const name = path.replace('dashboard.feature_previews.', '')
    if (opts?.ifUnset && name in server) throw new Error('already set')
    server[name] = value as boolean
    return { ok: true }
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('preview flag config paths', () => {
  it('names each flag by its snake_case config path', () => {
    expect(previewFlagConfigPath(PREVIEW_CREW)).toBe('dashboard.feature_previews.crew')
    expect(previewFlagConfigPath(PREVIEW_ARTIFACT_DEPLOY)).toBe('dashboard.feature_previews.artifact_deploy')
    expect(new Set(PREVIEW_FLAGS).size).toBe(PREVIEW_FLAGS.length)
  })

  it('keeps every declared flag on the gateway except the retired one', () => {
    const declared = Object.entries(previewFlags)
      .filter(([name, v]) => name.startsWith('PREVIEW_') && typeof v === 'string' && v.startsWith(PREVIEW_FLAG_PREFIX))
      .map(([, v]) => v as string)
      .filter(v => v !== PREVIEW_INSTANCE_SESSIONS && v !== PREVIEW_FLAG_PREFIX && v !== previewFlags.PREVIEW_FLAG_EVENT)
    expect([...PREVIEW_FLAGS].sort()).toEqual(declared.sort())
  })
})

describe('usePreviewFlagServerSync', () => {
  it('turns Crewmates on from the server when this browser has no copy', async () => {
    server = { crew: true }
    const { result } = mountSync(newClient())
    await waitFor(() => expect(result.current).toBe(true))
    expect(localStorage.getItem(PREVIEW_CREW)).toBe('1')
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('lets the server copy win over a stale browser copy', async () => {
    server = { crew: false }
    localStorage.setItem(PREVIEW_CREW, '1')
    localStorage.setItem(PREVIEW_FLAGS_SYNCED_KEY, '1')
    const { result } = mountSync(newClient())
    await waitFor(() => expect(result.current).toBe(false))
    expect(readPreviewFlag(PREVIEW_CREW)).toBe(false)
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('writes a toggle to the server', async () => {
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { expect(setPreviewFlag(PREVIEW_CREW, true)).toBe(true) })
    expect(result.current).toBe(true)
    await waitFor(() => expect(server).toEqual({ crew: true }))
    expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.crew', true, undefined)
    await waitFor(() => expect(qc.getQueryData<Body>(['kirocrewConfig'])?.dashboard?.feature_previews).toEqual({ crew: true }))
    expect(result.current).toBe(true)
  })

  it('does not flip a switch back while its write is still in flight', async () => {
    let release: () => void = () => {}
    patchSpy.mockImplementation(async (path: string, value: unknown) => {
      await new Promise<void>(r => { release = r })
      server[path.replace('dashboard.feature_previews.', '')] = value as boolean
      return { ok: true }
    })
    server = { crew: false }
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    // A config body read before the write landed still says off.
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current).toBe(true)
    await act(async () => { release() })
    await waitFor(() => expect(server.crew).toBe(true))
    await waitFor(() => expect(qc.getQueryData<Body>(['kirocrewConfig'])?.dashboard?.feature_previews).toEqual({ crew: true }))
    expect(result.current).toBe(true)
  })

  it('puts a refused write right from the server copy', async () => {
    server = { crew: false }
    patchSpy.mockRejectedValue(new Error('owner only'))
    const { result } = mountSync(newClient())
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    expect(result.current).toBe(true)
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('reports a toggle the gateway refused as owner-only on a 403', async () => {
    server = { crew: false }
    patchSpy.mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    expect(result.current).toBeNull()
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(result.current).toEqual({ flag: PREVIEW_CREW, denied: true }))
    act(() => { clearPreviewFlagWriteError() })
    expect(result.current).toBeNull()
  })

  it('reports any other refused toggle as a failed save, and clears it on the next accepted one', async () => {
    server = { crew: false }
    patchSpy.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(result.current).toEqual({ flag: PREVIEW_CREW, denied: false }))
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(result.current).toBeNull())
    expect(server.crew).toBe(true)
  })

  it('does not report a carry the gateway refused', async () => {
    server = { crew: false }
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    patchSpy.mockRejectedValue(Object.assign(new Error('already set'), { status: 409 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.webhooks', true, { ifUnset: true }))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current).toBeNull()
  })

  it('keeps the browser copy when the config cannot be read', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('offline'))
    localStorage.setItem(PREVIEW_CREW, '1')
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    expect(result.current).toBe(true)
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('carries a switch set before the server kept it up once, without replacing a stored one', async () => {
    server = { webhooks: false }
    localStorage.setItem(PREVIEW_CREW, '1')
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '0')
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(server.crew).toBe(true))
    expect(patchSpy).toHaveBeenCalledTimes(1)
    expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.crew', true, { ifUnset: true })
    // The stored webhooks value wins and an OFF copy is never carried.
    await waitFor(() => expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(false))
    expect(server.artifact_deploy).toBeUndefined()
    expect(result.current).toBe(true)
    // Later reads do not carry it again.
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(patchSpy).toHaveBeenCalledTimes(1)
  })

  it('does not retry a refused carry on every refetch', async () => {
    patchSpy.mockRejectedValue(new Error('owner only'))
    localStorage.setItem(PREVIEW_CREW, '1')
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(1))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(patchSpy).toHaveBeenCalledTimes(1)
    expect(result.current).toBe(true)
  })

  it('applies rapid toggles of one flag in click order', async () => {
    const order: boolean[] = []
    const releases: Array<() => void> = []
    patchSpy.mockImplementation(async (path: string, value: unknown) => {
      order.push(value as boolean)
      await new Promise<void>(r => { releases.push(r) })
      server[path.replace('dashboard.feature_previews.', '')] = value as boolean
      return { ok: true }
    })
    const { result } = mountSync(newClient())
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    act(() => { setPreviewFlag(PREVIEW_CREW, false) })
    // The second write waits for the first to settle.
    await waitFor(() => expect(order).toEqual([true]))
    await act(async () => { releases[0]() })
    await waitFor(() => expect(order).toEqual([true, false]))
    await act(async () => { releases[1]() })
    await waitFor(() => expect(server.crew).toBe(false))
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('reads a switch the server dropped as off once this browser has synced', async () => {
    server = { crew: true }
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(result.current).toBe(true))
    expect(localStorage.getItem(PREVIEW_FLAGS_SYNCED_KEY)).toBe('1')
    server = {}
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(result.current).toBe(false))
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('carries the retired Remote crew sessions switch up as Chat on a crew', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    mountSync(newClient())
    await waitFor(() => expect(server.remote_crew_chat).toBe(true))
    expect(patchSpy).toHaveBeenCalledTimes(1)
    expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.remote_crew_chat', true, { ifUnset: true })
    await waitFor(() => expect(readPreviewFlag(PREVIEW_INSTANCE_SESSIONS)).toBe(false))
    expect(readPreviewFlag(PREVIEW_REMOTE_CREW_CHAT)).toBe(true)
  })

  it('never lets the retired key turn Chat on a crew back on after it was turned off', async () => {
    server = { remote_crew_chat: false }
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    mountSync(newClient())
    await waitFor(() => expect(readPreviewFlag(PREVIEW_INSTANCE_SESSIONS)).toBe(false))
    expect(readPreviewFlag(PREVIEW_REMOTE_CREW_CHAT)).toBe(false)
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('sends no write for the retired key when Chat on a crew is turned off', async () => {
    server = { remote_crew_chat: true }
    mountSync(newClient())
    await waitFor(() => expect(readPreviewFlag(PREVIEW_REMOTE_CREW_CHAT)).toBe(true))
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    act(() => { setRemoteCrewChat(false) })
    await waitFor(() => expect(server.remote_crew_chat).toBe(false))
    expect(patchSpy).toHaveBeenCalledTimes(1)
    expect(readPreviewFlag(PREVIEW_INSTANCE_SESSIONS)).toBe(false)
  })

  it('writes nothing to the server once the shell unmounts', async () => {
    const { unmount } = mountSync(newClient())
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    unmount()
    expect(setPreviewFlag(PREVIEW_CREW, true)).toBe(true)
    expect(patchSpy).not.toHaveBeenCalled()
  })
})
