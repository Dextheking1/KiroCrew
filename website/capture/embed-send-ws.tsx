/**
 * Isolated capture entry for the ChatEmbed composer while a turn runs.
 *
 * Mounts the REAL ChatEmbed behind the real scoped app api, stylesheet, theme
 * tokens and i18n catalog. The capture script intercepts the network: the slot
 * read is a running turn, `POST /api/chat?ws=1` answers with the JSON receipt,
 * and the bare `POST /api/chat` never answers -- the gateway's SSE stream stays
 * open until the turn ends, so for a capture it never closes.
 *
 * Query params: `theme=light|dark`, `w=<px>` for the embed box width.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatEmbed from '../src/app-sdk/ChatEmbed'
import { AppScopedApiProvider } from '../src/app-sdk/scopedApi'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const width = Number(params.get('w') || 720)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
})

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="min-h-screen bg-bg text-text p-4" data-capture-root>
            <div style={{ width, height: 520 }} data-capture-box>
              <AppScopedApiProvider
                appName="capture-app"
                allowedApiPaths={['/api/chat']}
                allowedEvents={[]}
                navigateFn={() => {}}
              >
                <ChatEmbed slotKey="capture-app-embed" startAtBottom />
              </AppScopedApiProvider>
            </div>
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
