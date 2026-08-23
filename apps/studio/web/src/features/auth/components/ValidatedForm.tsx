import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { useRef } from 'react'
import type { AriaAttributes, FormHTMLAttributes, ReactNode } from 'react'

import { MOTION_DURATION_SECONDS } from '../../../components/ui/motion'

gsap.registerPlugin(useGSAP)

export type ValidationControlAria = Pick<
  AriaAttributes,
  'aria-describedby' | 'aria-invalid'
>

export interface ValidationFeedbackAttributes {
  'data-validation-feedback'?: 'invalid'
}

interface ValidatedFormProps<FieldName extends string>
  extends Omit<FormHTMLAttributes<HTMLFormElement>, 'noValidate'> {
  errors: Partial<Record<FieldName, string>>
  validationAttempt: number
}

interface ValidatedFieldProps {
  fieldName: string
  controlId: string
  label?: ReactNode
  error?: string
  className?: string
  errorClassName?: string
  children: (props: {
    controlAria: ValidationControlAria
    feedbackAttributes: ValidationFeedbackAttributes
  }) => ReactNode
}
export function ValidatedForm<FieldName extends string>({
  errors,
  validationAttempt,
  children,
  ...formProps
}: ValidatedFormProps<FieldName>) {
  const formRef = useRef<HTMLFormElement>(null)

  useGSAP(() => {
    if (validationAttempt === 0 || !Object.values(errors).some(Boolean)) return

    const firstInvalidControl = formRef.current?.querySelector<HTMLElement>(
      '[aria-invalid="true"]:not(:disabled)',
    )
    const invalidFeedbackTargets = formRef.current?.querySelectorAll<HTMLElement>(
      '[data-validation-feedback="invalid"]',
    )
    firstInvalidControl?.focus()
    if (!invalidFeedbackTargets?.length) return

    const media = gsap.matchMedia()
    media.add('(prefers-reduced-motion: no-preference)', () => {
      gsap.fromTo(invalidFeedbackTargets, { x: -5 }, {
        x: 5,
        duration: MOTION_DURATION_SECONDS.fast,
        ease: 'sine.inOut',
        repeat: 1,
        yoyo: true,
        overwrite: 'auto',
        clearProps: 'transform',
      })
    })
    return () => media.revert()
  }, {
    dependencies: [validationAttempt],
    scope: formRef,
    revertOnUpdate: true,
  })

  return <form {...formProps} ref={formRef} noValidate>{children}</form>
}

export function ValidatedField({
  fieldName,
  controlId,
  label,
  error,
  className,
  errorClassName,
  children,
}: ValidatedFieldProps) {
  const errorId = `${controlId}-error`
  const isInvalid = Boolean(error)

  return (
    <div className={className} data-validation-field={fieldName}>
      {label !== undefined && <label htmlFor={controlId}>{label}</label>}
      {children({
        controlAria: {
          'aria-invalid': isInvalid,
          'aria-describedby': isInvalid ? errorId : undefined,
        },
        feedbackAttributes: {
          'data-validation-feedback': isInvalid ? 'invalid' : undefined,
        },
      })}
      {error && (
        <small
          className={['validated-field-error', errorClassName].filter(Boolean).join(' ')}
          id={errorId}
          role="alert"
        >
          {error}
        </small>
      )}
    </div>
  )
}
