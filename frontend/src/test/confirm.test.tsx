import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { ConfirmProvider, useConfirm } from '../components/Confirm'

function Probe({ onAnswer }: { onAnswer: (value: boolean) => void }) {
  const confirm = useConfirm()
  return (
    <button type="button" onClick={async () => onAnswer(await confirm('Remove this?'))}>
      ask
    </button>
  )
}

describe('confirmation box', () => {
  it('answers yes only when the person says so, and no on cancel or Escape', async () => {
    const answers: boolean[] = []
    render(
      <ConfirmProvider>
        <Probe onAnswer={(v) => answers.push(v)} />
      </ConfirmProvider>,
    )
    await userEvent.click(screen.getByRole('button', { name: 'ask' }))
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Remove this?')
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus() // the safe answer is the default
    await userEvent.click(screen.getByRole('button', { name: 'Yes, continue' }))
    await userEvent.click(screen.getByRole('button', { name: 'ask' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await userEvent.click(screen.getByRole('button', { name: 'ask' }))
    await userEvent.keyboard('{Escape}')
    await vi.waitFor(() => expect(answers).toEqual([true, false, false]))
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })
})
