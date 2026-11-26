import { useEffect, useRef, useState } from 'react'

/** Whether the query matches. `onChange` runs when the window crosses the query, not on first render. */
export function useMediaQuery(query: string, onChange?: (matches: boolean) => void): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)
  const onChangeRef = useRef(onChange)

  useEffect(() => {
    onChangeRef.current = onChange
  }, [onChange])

  useEffect(() => {
    const list = window.matchMedia(query)
    const listener = () => {
      setMatches(list.matches)
      onChangeRef.current?.(list.matches)
    }
    list.addEventListener('change', listener)
    return () => list.removeEventListener('change', listener)
  }, [query])

  return matches
}
