import { X } from 'lucide-react'
import { forwardRef } from 'react'
import type { ReactNode } from 'react'

import { IconButton } from './IconButton'

export interface DrawerHeaderProps {
  title: ReactNode
  description?: ReactNode
  closeLabel?: string
  onClose?: () => void
  className?: string
}

export const DrawerHeader = forwardRef<HTMLButtonElement, DrawerHeaderProps>(function DrawerHeader({
  title,
  description,
  closeLabel,
  onClose,
  className,
}, ref) {
  const classes = [
    'ui-drawer-header',
    description ? 'has-description' : '',
    className,
  ].filter(Boolean).join(' ')

  return (
    <header className={classes}>
      <span className="ui-drawer-header__heading">
        <h2>{title}</h2>
        {description && <span>{description}</span>}
      </span>
      {onClose && closeLabel && (
        <IconButton ref={ref} label={closeLabel} icon={<X size={18} />} onClick={onClose} />
      )}
    </header>
  )
})
