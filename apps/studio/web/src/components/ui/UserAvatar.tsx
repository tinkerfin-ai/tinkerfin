import { useMemo, useState } from 'react'

export interface UserAvatarProps {
  avatarUrl: string | null
  displayName: string
  username: string
  size?: 'sm' | 'lg'
  className?: string
}

export function UserAvatar({
  avatarUrl,
  displayName,
  username,
  size = 'sm',
  className,
}: UserAvatarProps) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const fallback = useMemo(() => (
    Array.from(displayName.trim() || username.trim())[0]?.toLocaleUpperCase() ?? '?'
  ), [displayName, username])
  const canRenderImage = Boolean(avatarUrl && avatarUrl !== failedUrl)

  return (
    <span
      className={`user-avatar user-avatar--${size}${className ? ` ${className}` : ''}`}
      aria-hidden="true"
    >
      {canRenderImage ? (
        <img
          src={avatarUrl ?? undefined}
          alt=""
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailedUrl(avatarUrl)}
        />
      ) : (
        <span className="user-avatar__fallback">{fallback}</span>
      )}
    </span>
  )
}
