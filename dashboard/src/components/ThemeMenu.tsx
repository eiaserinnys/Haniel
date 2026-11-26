import { useEffect, useRef, useState } from 'react'
import { Icon, type IconName } from './Icon'
import { applyTheme, readThemePreference, saveThemePreference, type ThemePreference } from '@/lib/theme'

const ITEMS: Array<[ThemePreference, string, IconName]> = [
  ['light', 'Light', 'sun'],
  ['dark', 'Dark', 'moon'],
  ['system', 'System', 'system'],
]

export function ThemeMenu() {
  const [preference, setPreference] = useState<ThemePreference>(readThemePreference)
  const [open, setOpen] = useState(false)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    applyTheme(preference)
  }, [preference])

  useEffect(() => {
    if (!open) return
    menuRef.current?.querySelector<HTMLElement>('[aria-checked="true"]')?.focus()
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node
      if (!menuRef.current?.contains(target) && !buttonRef.current?.contains(target)) setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open])

  const close = () => {
    setOpen(false)
    buttonRef.current?.focus()
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      close()
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const items = [...(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]') ?? [])]
      const i = items.indexOf(document.activeElement as HTMLElement)
      items[(i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus()
    } else if (e.key === 'Tab') {
      setOpen(false)
    }
  }

  const choose = (value: ThemePreference) => {
    setPreference(value)
    saveThemePreference(value)
    close()
  }

  const [, label, icon] = ITEMS.find(([value]) => value === preference) ?? ITEMS[2]

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="icon-btn"
        aria-label={`Theme: ${label}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name={icon} />
      </button>
      {open && (
        <div ref={menuRef} className="menu" role="menu" aria-label="Theme" onKeyDown={onKeyDown}>
          {ITEMS.map(([value, text, name]) => (
            <button
              key={value}
              type="button"
              className="menu-item"
              role="menuitemradio"
              aria-checked={preference === value}
              onClick={() => choose(value)}
            >
              <Icon name={name} />
              <span>{text}</span>
              {preference === value ? <Icon name="check" className="tick" /> : <span />}
            </button>
          ))}
        </div>
      )}
    </>
  )
}
