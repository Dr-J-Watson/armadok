import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { ScheduleTab } from '../features/schedule/ScheduleTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const task = (over = {}) => ({
  id: 't1', name: 'Night clean', action: 'docker.prune', target: { node_id: 'n1', kind: 'images' },
  schedule: { kind: 'daily', at: '03:00', timezone: 'UTC', weekdays: [0, 4] }, enabled: true,
  next_run: '2030-01-01T03:00:00Z', last_run: '2029-12-31T03:00:00Z', last_result: 'ok', ...over,
})
const actions = [
  { name: 'docker.prune', fields: [{ name: 'kind', choices: ['containers', 'images', 'networks'] }] },
  { name: 'project.restart', fields: [{ name: 'project', choices: [] }] },
]

function renderTab(tasks: unknown[], canManage = true) {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) =>
    init?.method && init.method !== 'GET' ? json({ result: 'ok' }) : json({ tasks, actions, can_manage: canManage }),
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ScheduleTab nodeId="n1" />
    </QueryClientProvider>,
  )
  return spy
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('scheduled tasks tab', () => {
  it('describes each task in words, with its last result', async () => {
    renderTab([task(), task({ id: 't2', name: 'Broken', last_result: 'schedule.creator_lost_right' })], false)
    expect(await screen.findByText('Night clean')).toBeInTheDocument()
    expect(screen.getAllByText(/Clean up Docker/).length).toBe(2)
    expect(screen.getAllByText(/Unused \(dangling\) images/).length).toBe(2)
    expect(screen.getAllByText(/at 03:00 \(UTC\), Mon, Fri/).length).toBe(2)
    expect(screen.getByText(/no longer holds the right it needs/)).toBeInTheDocument()
  })

  it('shows the controls only to those who can manage', async () => {
    renderTab([task()], false)
    await screen.findByText('Night clean')
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull()
    expect(screen.queryByRole('form', { name: 'New scheduled task' })).toBeNull()
  })

  it('builds the form from the action it is given and sends the schedule', async () => {
    const spy = renderTab([])
    await screen.findByText('No scheduled task yet.')
    await userEvent.type(screen.getByLabelText('Name'), 'Clean')
    await userEvent.selectOptions(screen.getByLabelText('Action'), 'docker.prune')
    await userEvent.selectOptions(await screen.findByLabelText('What to remove'), 'images')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))
    const post = spy.mock.calls.find(([, init]) => init?.method === 'POST')
    const body = JSON.parse(String(post![1]!.body))
    expect(body).toMatchObject({ name: 'Clean', action: 'docker.prune', target: { kind: 'images' } })
    expect(body.schedule).toMatchObject({ kind: 'daily', at: '03:00', weekdays: [] })
  })

  it('runs a task now and says how it went', async () => {
    renderTab([task()])
    await userEvent.click(await screen.findByRole('button', { name: 'Run now' }))
    expect(await screen.findByText('The task ran.')).toBeInTheDocument()
  })
})
