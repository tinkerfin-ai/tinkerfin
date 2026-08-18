import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { useRef } from 'react'

import { BrandMark } from '../../components/BrandMark'

gsap.registerPlugin(useGSAP)

export function LoginTransition({ onComplete }: { onComplete: () => void }) {
  const rootRef = useRef<HTMLElement>(null)
  const onCompleteRef = useRef(onComplete)
  onCompleteRef.current = onComplete

  useGSAP(() => {
    const root = rootRef.current
    const mark = root?.querySelector<HTMLElement>('.login-transition__mark')
    if (!root || !mark) return

    const timeline = gsap.timeline({
      onComplete: () => onCompleteRef.current(),
    })
    timeline
      .fromTo(root, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.12, ease: 'power1.out' })
      .fromTo(mark, { scale: 0.84, rotate: -8 }, { scale: 1, rotate: 0, duration: 0.2, ease: 'power2.out' }, 0)
      .to(root, { autoAlpha: 0, duration: 0.28, ease: 'power1.in' }, 0.12)

    return () => timeline.kill()
  }, { scope: rootRef })

  return (
    <main ref={rootRef} className="login-transition" aria-label="正在进入工作区">
      <span className="login-transition__mark" aria-hidden="true"><BrandMark size={28} /></span>
    </main>
  )
}
