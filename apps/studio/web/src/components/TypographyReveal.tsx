import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { SplitText } from 'gsap/SplitText'
import { useRef } from 'react'
import type { HTMLAttributes } from 'react'

gsap.registerPlugin(useGSAP, SplitText)

const REVEAL_TIMING = {
  display: { duration: 0.52, stagger: 0.06 },
  state: { duration: 0.32, stagger: 0.04 },
} as const

export interface TypographyRevealProps extends HTMLAttributes<HTMLHeadingElement> {
  as: 'h1' | 'h2' | 'h3'
  variant: keyof typeof REVEAL_TIMING
  revealKey?: string | number
}

export function TypographyReveal({
  as: Heading,
  variant,
  revealKey,
  className,
  ...headingProps
}: TypographyRevealProps) {
  const headingRef = useRef<HTMLHeadingElement>(null)

  useGSAP(() => {
    const heading = headingRef.current
    if (!heading) return

    const media = gsap.matchMedia()
    media.add('(prefers-reduced-motion: no-preference)', () => {
      SplitText.create(heading, {
        type: 'lines',
        mask: 'lines',
        autoSplit: true,
        aria: 'auto',
        onSplit(split) {
          return gsap.from(split.lines, {
            yPercent: 105,
            autoAlpha: 0,
            duration: REVEAL_TIMING[variant].duration,
            ease: 'power3.out',
            stagger: REVEAL_TIMING[variant].stagger,
            clearProps: 'transform,opacity,visibility',
          })
        },
      })
    })

    return () => media.revert()
  }, {
    dependencies: [variant, revealKey],
    scope: headingRef,
    revertOnUpdate: true,
  })

  const classes = [
    'typography-heading',
    `typography-reveal--${variant}`,
    className,
  ].filter(Boolean).join(' ')

  return <Heading ref={headingRef} className={classes} {...headingProps} />
}
