import { render, screen } from '@testing-library/react'
import '../core/i18n'
import { HelpPage } from '../features/help/HelpPage'

describe('help page', () => {
  it('explains the first steps and the words', () => {
    render(<HelpPage />)
    expect(screen.getByRole('heading', { name: 'Help' })).toBeInTheDocument()
    expect(screen.getByText('Worker', { selector: 'dt' })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem').length).toBeGreaterThan(8)
  })
})
