const PATHS = {
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </>
  ),
  moon: <path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z" />,
  system: (
    <>
      <rect x="3" y="4" width="18" height="12" />
      <path d="M8 20h8M12 16v4" />
    </>
  ),
  check: <path d="M20 6 9 17l-5-5" />,
  chev: <path d="m9 6 6 6-6 6" />,
  x: <path d="M18 6 6 18M6 6l12 12" />,
  restart: (
    <>
      <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
      <path d="M3 3v5h5" />
    </>
  ),
  reload: (
    <>
      <path d="M21 12a9 9 0 0 0-15-6.7L3 8" />
      <path d="M3 3v5h5" />
      <path d="M3 12a9 9 0 0 0 15 6.7L21 16" />
      <path d="M16 16h5v5" />
    </>
  ),
  stop: <rect x="6" y="6" width="12" height="12" />,
  play: <path d="M7 4v16l13-8Z" />,
  zap: <path d="M13 2 4 14h8l-1 8 9-12h-8Z" />,
  logs: (
    <>
      <path d="m4 17 6-5-6-5" />
      <path d="M12 19h8" />
    </>
  ),
  edit: <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />,
  trash: <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6" />,
  plus: <path d="M12 5v14M5 12h14" />,
  loader: <path d="M12 3a9 9 0 1 0 9 9" />,
} as const

export type IconName = keyof typeof PATHS

interface IconProps {
  name: IconName
  small?: boolean
  className?: string
}

export function Icon({ name, small, className }: IconProps) {
  const size = small ? ' ico-sm' : ''
  return (
    <svg className={`ico${size}${className ? ` ${className}` : ''}`} viewBox="0 0 24 24" aria-hidden="true">
      {PATHS[name]}
    </svg>
  )
}
