import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useTodoTraceDrawer } from './useTodoTraceDrawer'
import { beforeEach, describe, expect, it } from 'vitest'

const taskTrace = {
  phase: 'ready' as const,
  snapshot: {
    status: 'ready' as const,
    todoGroups: [{
      id: 'todo-group:run-1',
      userMessageId: 'message-1',
      userMessagePreview: '任务',
      groupToolCallId: 'tool-1',
      createdAt: '2026-08-31T00:00:00.000Z',
      status: 'running' as const,
      todos: [],
    }],
  },
}

function Harness({ threadId = 'thread-1', blocked = false }) {
  const drawer = useTodoTraceDrawer({ threadId, taskTrace, blocked })
  return (
    <>
      <button ref={drawer.launcherRef} type="button" onClick={drawer.toggle}>
        launcher
      </button>
      <aside ref={drawer.drawerRef}>
        <button type="button">first</button>
        <button type="button">last</button>
      </aside>
      <output data-testid="drawer-state">
        {JSON.stringify({
          open: drawer.open,
          desiredOpen: drawer.desiredOpen,
          modalActive: drawer.modalActive,
        })}
      </output>
    </>
  )
}

const state = () => JSON.parse(screen.getByTestId('drawer-state').textContent ?? '{}') as {
  open: boolean
  desiredOpen: boolean
  modalActive: boolean
}

describe('useTodoTraceDrawer', () => {
  beforeEach(() => window.sessionStorage.clear())

  it('separates a user open preference from temporary interaction blocking', () => {
    const { rerender } = render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'launcher' }))
    expect(state()).toMatchObject({ open: true, desiredOpen: true, modalActive: true })

    rerender(<Harness blocked />)
    expect(state()).toMatchObject({ open: false, desiredOpen: true, modalActive: false })

    rerender(<Harness />)
    expect(state()).toMatchObject({ open: true, desiredOpen: true, modalActive: true })
  })

  it('keeps overlay focus inside launcher and drawer and restores it on Escape', async () => {
    render(<Harness />)
    const launcher = screen.getByRole('button', { name: 'launcher' })
    fireEvent.click(launcher)
    const last = screen.getByRole('button', { name: 'last' })
    last.focus()

    fireEvent.keyDown(document, { key: 'Tab' })
    expect(launcher).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(state().open).toBe(false)
    await waitFor(() => expect(launcher).toHaveFocus())
  })

  it('stores the explicit preference per conversation', async () => {
    const { rerender } = render(<Harness threadId="thread-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'launcher' }))
    expect(state().open).toBe(true)

    rerender(<Harness threadId="thread-2" />)
    await waitFor(() => expect(state().open).toBe(false))

    rerender(<Harness threadId="thread-1" />)
    await waitFor(() => expect(state().open).toBe(true))
  })

  it('closes a docked drawer with Escape without installing a Tab trap', () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
    render(<Harness />)
    const launcher = screen.getByRole('button', { name: 'launcher' })
    fireEvent.click(launcher)
    expect(state()).toMatchObject({ open: true, modalActive: false })

    const last = screen.getByRole('button', { name: 'last' })
    last.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(last).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(state().open).toBe(false)
  })
})
