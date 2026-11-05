import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { NotFound } from '../components/NotFound'

let explode = true
function Bomb() {
  if (explode) throw new Error('boom')
  return <p>fine now</p>
}

describe('error boundary', () => {
  it('shows a calm message instead of a blank page, and lets the person try again', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong on this screen')
    explode = false
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(screen.getByText('fine now')).toBeInTheDocument()
  })
})

describe('page not found', () => {
  it('says so kindly and links back to the overview', () => {
    render(
      <MemoryRouter>
        <NotFound />
      </MemoryRouter>,
    )
    expect(screen.getByRole('heading', { name: 'This page does not exist' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Overview' })).toHaveAttribute('href', '/dashboard')
  })
})
