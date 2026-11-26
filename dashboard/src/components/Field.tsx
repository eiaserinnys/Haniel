import type { ReactNode } from 'react'

interface FieldProps {
  label: string
  required?: boolean
  hint?: string
  hintIsText?: boolean
  children: ReactNode
}

export function Field({ label, required, hint, hintIsText, children }: FieldProps) {
  return (
    <label className="field">
      <span className="field-label">
        {label}
        {required && (
          <span className="req" aria-hidden="true">
            *
          </span>
        )}
        {hint && <span className={`field-hint${hintIsText ? ' is-text' : ''}`}>{hint}</span>}
      </span>
      {children}
    </label>
  )
}
