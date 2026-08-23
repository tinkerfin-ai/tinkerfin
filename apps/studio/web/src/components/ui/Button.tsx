import { LoaderCircle } from 'lucide-react'
import { forwardRef } from 'react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'text'
export type ButtonSize = 'sm' | 'md' | 'lg' | 'xl'
export type ButtonShape = 'round' | 'capsule' | 'circle'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  shape?: ButtonShape
  loading?: boolean
  selected?: boolean
  leadingIcon?: ReactNode
  trailingIcon?: ReactNode
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({
  variant = 'secondary',
  size = 'lg',
  shape = 'round',
  loading = false,
  selected = false,
  leadingIcon,
  trailingIcon,
  type = 'button',
  disabled,
  className,
  children,
  ...buttonProps
}, ref) {
  const classes = [
    'ui-button',
    `ui-button--${variant}`,
    `ui-button--${size}`,
    `ui-button--${shape}`,
    selected ? 'is-selected' : '',
    className,
  ].filter(Boolean).join(' ')

  return (
    <button
      {...buttonProps}
      ref={ref}
      className={classes}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      aria-pressed={buttonProps['aria-pressed'] ?? (selected ? true : undefined)}
    >
      {loading
        ? <LoaderCircle className="ui-button__spinner" size={16} aria-hidden="true" />
        : leadingIcon && <span className="ui-button__icon" aria-hidden="true">{leadingIcon}</span>}
      {children !== undefined && <span className="ui-button__label">{children}</span>}
      {!loading && trailingIcon && <span className="ui-button__icon" aria-hidden="true">{trailingIcon}</span>}
    </button>
  )
})
