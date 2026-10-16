/** The author of a user message in a shared session, from the server's
 *  `meta.author` stamp. Shown only when someone other than the viewer wrote it,
 *  so a single-person Crew's transcript is unchanged. */
import { messageAuthor, principalName } from '../../../api/sharedCrewUi'
import { useSharedCrewViewer } from '../../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../../i18n/t'

function AuthoredBy({ authorId, name }: { authorId: string; name: string }) {
  const viewer = useSharedCrewViewer()
  if (!viewer.isSuccess || viewer.data?.id === authorId) return null
  return (
    <div className="text-[12px] leading-5 text-muted mb-0.5 pr-1 truncate max-w-full" data-testid="message-byline" title={i18nT('sharedCrew.sent_by', { name })}>
      {name}
    </div>
  )
}

export function MessageByline({ meta }: { meta?: Record<string, unknown> }) {
  const author = messageAuthor(meta)
  if (!author) return null
  return <AuthoredBy authorId={author.id} name={principalName(author) || i18nT('sharedCrew.unknown_person')} />
}
