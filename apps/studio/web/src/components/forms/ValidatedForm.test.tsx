import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ValidatedField, ValidatedForm } from './ValidatedForm'

const animationMocks = vi.hoisted(() => ({
  add: vi.fn<(query: string, callback: () => void) => void>(),
  fromTo: vi.fn(),
  registerPlugin: vi.fn(),
  revert: vi.fn(),
}))

vi.mock('gsap', () => ({
  default: {
    fromTo: animationMocks.fromTo,
    matchMedia: () => ({
      add: animationMocks.add,
      revert: animationMocks.revert,
    }),
    registerPlugin: animationMocks.registerPlugin,
  },
}))

vi.mock('@gsap/react', async () => {
  const React = await vi.importActual<typeof import('react')>('react')
  return {
    useGSAP: (callback: React.EffectCallback) => React.useLayoutEffect(callback),
  }
})

interface HarnessProps {
  errors?: Partial<Record<'first' | 'second', string>>
}

function Harness({
  errors = { first: '请输入第一项', second: '请输入第二项' },
}: HarnessProps) {
  const [validationAttempt, setValidationAttempt] = useState(0)

  return (
    <ValidatedForm
      aria-label="测试表单"
      errors={errors}
      validationAttempt={validationAttempt}
      onSubmit={(event) => {
        event.preventDefault()
        setValidationAttempt((current) => current + 1)
      }}
    >
      <ValidatedField fieldName="first" controlId="first" label="第一项" error={errors.first}>
        {({ controlAria, feedbackAttributes }) => (
          <span {...feedbackAttributes}><input id="first" {...controlAria} /></span>
        )}
      </ValidatedField>
      <ValidatedField fieldName="second" controlId="second" label="第二项" error={errors.second}>
        {({ controlAria, feedbackAttributes }) => (
          <span {...feedbackAttributes}><input id="second" {...controlAria} /></span>
        )}
      </ValidatedField>
      <button type="submit">提交</button>
    </ValidatedForm>
  )
}

describe('ValidatedForm', () => {
  beforeEach(() => {
    animationMocks.add.mockReset()
    animationMocks.add.mockImplementation((_query, callback) => callback())
    animationMocks.fromTo.mockReset()
    animationMocks.revert.mockReset()
  })

  it('suppresses native validation and associates inline field errors', () => {
    render(<Harness />)

    expect(screen.getByRole('form', { name: '测试表单' })).toHaveAttribute('novalidate')
    expect(screen.getByLabelText('第一项')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('第一项')).toHaveAccessibleDescription('请输入第一项')
    expect(screen.getByLabelText('第二项')).toHaveAccessibleDescription('请输入第二项')
  })

  it('focuses the first invalid control and repeats feedback on every invalid submit', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    await user.click(screen.getByRole('button', { name: '提交' }))

    expect(screen.getByLabelText('第一项')).toHaveFocus()
    expect(animationMocks.fromTo).toHaveBeenCalledTimes(1)
    expect(animationMocks.fromTo.mock.calls[0][2]).toMatchObject({
      clearProps: 'transform',
      duration: 0.07,
      repeat: 3,
      yoyo: true,
    })

    await user.click(screen.getByRole('button', { name: '提交' }))
    expect(animationMocks.fromTo).toHaveBeenCalledTimes(2)
  })

  it('keeps focus feedback but skips shaking when reduced motion is requested', async () => {
    const user = userEvent.setup()
    animationMocks.add.mockImplementation(() => undefined)
    render(<Harness />)

    await user.click(screen.getByRole('button', { name: '提交' }))

    await waitFor(() => expect(screen.getByLabelText('第一项')).toHaveFocus())
    expect(animationMocks.fromTo).not.toHaveBeenCalled()
  })

  it('does not move focus or animate when the form has no errors', async () => {
    const user = userEvent.setup()
    render(<Harness errors={{}} />)
    const submitButton = screen.getByRole('button', { name: '提交' })

    await user.click(submitButton)

    expect(submitButton).toHaveFocus()
    expect(animationMocks.fromTo).not.toHaveBeenCalled()
  })
})
