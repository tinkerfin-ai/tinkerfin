import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { useRef } from 'react'
import type { FormHTMLAttributes } from 'react'

import { MOTION_DURATION_SECONDS } from './motion'

gsap.registerPlugin(useGSAP)

interface ValidatedFormProps<FieldName extends string>
  extends Omit<FormHTMLAttributes<HTMLFormElement>, 'noValidate'> {
  errors: Partial<Record<FieldName, string>>
  validationAttempt: number
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
