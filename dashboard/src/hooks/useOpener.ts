import { useState } from 'react'

/**
 * Radix returns focus to its own Trigger on close. Our layers are opened by plain buttons,
 * so remember the element that had focus at mount and give it back on close.
 */
export function useOpener(): (event: Event) => void {
  const [opener] = useState(() => document.activeElement as HTMLElement | null)
  return (event) => {
    event.preventDefault()
    if (opener?.isConnected) opener.focus()
  }
}
