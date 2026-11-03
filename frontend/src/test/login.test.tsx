import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { ApiError } from '../core/api'
import * as auth from '../core/auth'
import { LoginPage } from '../features/auth/LoginPage'

function renderLogin(login: (u: string, p: string, c?: string) => Promise<void>) {
  vi.spyOn(auth, 'useAuth').mockReturnValue({
    state: { status: 'anonymous' },
    login,
    setup: vi.fn(),
    logout: vi.fn(),
  })
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <LoginPage />
    </QueryClientProvider>,
  )
}

describe('LoginPage', () => {
  it('shows a translated message for a server error code', async () => {
    renderLogin(() => Promise.reject(new ApiError('auth.invalid_credentials', 401)))
    await userEvent.type(screen.getByLabelText('Username'), 'admin')
    await userEvent.type(screen.getByLabelText('Password'), 'wrong password')
    await userEvent.click(screen.getByRole('button', { name: 'Log in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong username, password or code.')
  })

  it('asks for the second factor only when the server requires it', async () => {
    const login = vi
      .fn()
      .mockRejectedValueOnce(new ApiError('auth.totp_required', 401))
      .mockResolvedValueOnce(undefined)
    renderLogin(login)
    await userEvent.type(screen.getByLabelText('Username'), 'admin')
    await userEvent.type(screen.getByLabelText('Password'), 'correct horse battery')
    expect(screen.queryByLabelText('Authentication code')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Log in' }))
    await userEvent.type(await screen.findByLabelText('Authentication code'), '123456')
    await userEvent.click(screen.getByRole('button', { name: 'Log in' }))
    expect(login).toHaveBeenLastCalledWith('admin', 'correct horse battery', '123456')
  })
})
