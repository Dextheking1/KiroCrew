import { useContext, useSyncExternalStore } from 'react'
import { ReactReduxContext } from 'react-redux'
import type { RootState } from '../store'

const noopSubscribe = () => () => {}

/**
 * Whether a remote Crew's embedded dashboard currently fills the window.
 *
 * Switching to a remote Crew HIDES the local dashboard (`display:none` on its
 * wrapper in `App`) rather than unmounting it, so local state survives the
 * switch. Anything the local dashboard portals to `document.body` escapes that
 * wrapper and stays on screen over the remote pane; such overlays read this to
 * hide themselves while another Crew is showing.
 *
 * Reads the store through the context rather than `useAppSelector` so a
 * component rendered without a Redux provider (isolated tests, previews)
 * reports the local dashboard as showing instead of throwing.
 */
export function useRemoteCrewActive(): boolean {
  const store = useContext(ReactReduxContext)?.store
  return useSyncExternalStore(
    store ? store.subscribe : noopSubscribe,
    () => (store ? (store.getState() as RootState).instances?.activeId != null : false),
  )
}
