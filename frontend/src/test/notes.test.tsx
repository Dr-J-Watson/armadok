import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { NotesPanel } from '../features/notes/NotesPanel'
import type { Note } from '../features/notes/NotesPanel'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
let notes: Note[] = []
const sent: { method: string; path: string; body?: Record<string, unknown> }[] = []
let conflict = false

const note = (over: Partial<Note> = {}): Note => ({
  id: 'k1', node_id: 'n1', object_type: 'container', object_id: 'c1', body: '<b>renew</b> the cert',
  importance: 'todo', pinned: false, done: false, remind_at: null, updated_at: 'u1', ...over,
})

function server() {
  sent.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method !== 'GET') sent.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    if (method === 'GET') return json(notes)
    if (method === 'PATCH' && conflict) return json({ error: { code: 'notes.conflict' } }, 409)
    return json({})
  })
}

function renderPanel() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NotesPanel nodeId="n1" objectType="container" objectId="c1" />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
  notes = []
  conflict = false
})

describe('notes panel', () => {
  it('says notes are private and shows a note as plain text, never as markup', async () => {
    notes = [note()]
    server()
    const { container } = renderPanel()
    expect(screen.getByText(/only you can see them/)).toBeInTheDocument()
    expect(await screen.findByText('<b>renew</b> the cert')).toBeInTheDocument()
    expect(container.querySelector('b')).toBeNull() // the tag is text, not an element
  })

  it('adds a note to this object with its importance', async () => {
    server()
    renderPanel()
    await userEvent.type(await screen.findByLabelText('New note'), 'call the provider')
    await userEvent.selectOptions(screen.getByLabelText('Importance'), 'warning')
    await userEvent.click(screen.getByRole('button', { name: 'Add the note' }))
    await vi.waitFor(() => expect(sent.some((s) => s.method === 'POST')).toBe(true))
    expect(sent.find((s) => s.method === 'POST')?.body).toMatchObject({
      node_id: 'n1', object_type: 'container', object_id: 'c1', body: 'call the provider', importance: 'warning',
    })
  })

  it('marks done and pins, naming the version it edited', async () => {
    notes = [note()]
    server()
    renderPanel()
    await userEvent.click(await screen.findByRole('checkbox', { name: 'Done' }))
    await vi.waitFor(() => expect(sent.length).toBe(1))
    expect(sent[0].body).toEqual({ done: true, expected_updated_at: 'u1' })
    await userEvent.click(screen.getByRole('button', { name: 'Pin' }))
    await vi.waitFor(() => expect(sent.length).toBe(2))
    expect(sent[1].body).toMatchObject({ pinned: true })
  })

  it('tells the user when the note was changed elsewhere instead of overwriting', async () => {
    notes = [note()]
    conflict = true
    server()
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Edit' }), ' more')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('changed elsewhere')
  })
})

describe('sharing a note', () => {
  it('shows who it is shared with and lets the author add someone who sees the node', async () => {
    notes = [note()]
    sent.length = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = String(input).replace('/api/v1', '')
      const method = (init?.method ?? 'GET').toUpperCase()
      if (method !== 'GET') sent.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      if (path.startsWith('/notes/people')) return json([{ id: 'u2', username: 'carl' }])
      if (path.endsWith('/shares') && method === 'GET') return json([])
      return method === 'GET' ? json(notes) : json({})
    })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Share' }))
    expect(await screen.findByText('Private: only you can read this note.')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Show it to (exact user name)'), 'carl')
    await userEvent.click(screen.getByRole('button', { name: 'Look up' }))
    await userEvent.click(await screen.findByRole('button', { name: 'carl' }))
    expect(sent.find((c) => c.path === '/notes/k1/shares')?.body).toEqual({ user_id: 'u2' })
  })

  it('shows a note shared with me as read-only', async () => {
    notes = [note({ read_only: true, author: 'bob' })]
    server()
    renderPanel()
    expect(await screen.findByText('Shared by bob (read-only)')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Share' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  })
})

