import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { CommandPalette } from '../features/palette/CommandPalette'

// The palette reads which pages the account may open and which features the server has.
vi.mock('../core/auth', () => ({ useCanFn: () => () => true }))
vi.mock('../core/useFeatures', () => ({
  useFeatures: () => [{ name: 'x', nav: [{ id: 'docs', labelKey: 'nav.nodes', to: '/nodes' }] }],
}))

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>
}

function renderPalette(results: unknown[] = []) {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json({ results }))
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <CommandPalette />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return spy
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('command palette', () => {
  it('opens with Ctrl+K and closes with Escape', async () => {
    renderPalette()
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('asks the server only once at least two letters are typed, and goes where a result leads', async () => {
    const spy = renderPalette([{ type: 'node', label: 'blog-server', detail: 'docker', to: '/nodes/abc' }])
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    const box = await screen.findByRole('combobox')
    await userEvent.type(box, 'b')
    expect(spy).not.toHaveBeenCalled()
    await userEvent.type(box, 'lo')
    const option = await screen.findByRole('option', { name: /blog-server/ })
    expect(String(spy.mock.calls[0][0])).toMatch(/\/search$/) // the text is in the body, not the address
    expect(JSON.parse(String(spy.mock.calls[0][1]!.body))).toEqual({ q: 'blo' })
    await userEvent.click(option)
    expect(screen.getByTestId('where')).toHaveTextContent('/nodes/abc')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('does not follow a path that leaves the application', async () => {
    renderPalette([{ type: 'node', label: 'evil', detail: '', to: '//example.com/x' }])
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    await userEvent.type(await screen.findByRole('combobox'), 'evil')
    await userEvent.click(await screen.findByRole('option', { name: /evil/ }))
    expect(screen.getByTestId('where')).toHaveTextContent('/')
    expect(screen.getByTestId('where')).not.toHaveTextContent('example.com')
  })

  it('says when nothing matches', async () => {
    renderPalette([])
    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    await userEvent.type(await screen.findByRole('combobox'), 'zzzz')
    expect(await screen.findByText('Nothing found.')).toBeInTheDocument()
  })
})

describe('pages', () => {
  it('lists the pages the account may open and filters them as you type', async () => {
    renderPalette()
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    expect(await screen.findByRole('option', { name: /Machines/ })).toBeInTheDocument()
    await userEvent.type(screen.getByRole('combobox'), 'zz')
    expect(screen.queryByRole('option', { name: /Machines/ })).toBeNull()
  })
})
