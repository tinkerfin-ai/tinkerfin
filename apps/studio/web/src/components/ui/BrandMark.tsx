import { Blocks } from 'lucide-react'

export function BrandMark({ size = 22, className }: { size?: number; className?: string } = {}) {
  return (
    <span className={className ? `brand-mark ${className}` : 'brand-mark'} aria-hidden="true"><Blocks size={size} strokeWidth={2.2} /></span>
  )
}
