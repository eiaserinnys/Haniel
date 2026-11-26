import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

/**
 * The section the page is currently on: the last one whose head has passed the middle of the scroll
 * area; Services at the very top; the last section once the end is reached. The middle (not the top edge) keeps a short section
 * such as Repositories lit for a while before the page runs out.
 *
 * `jump` scrolls to a section and keeps it current while the page stays where the jump left it, since a
 * short last section cannot reach the top edge. Any other scroll goes back to the position rule.
 */
export function useCurrentSection<T extends string>(scrollRef: RefObject<HTMLElement | null>, ids: readonly T[]) {
  const [current, setCurrent] = useState<T>(ids[0])
  const idsRef = useRef(ids)
  const pinRef = useRef<{ id: T; top: number } | null>(null)

  useEffect(() => {
    const scroller = scrollRef.current
    if (!scroller) return
    const update = () => {
      const pin = pinRef.current
      if (pin && Math.abs(scroller.scrollTop - pin.top) <= 2) {
        setCurrent(pin.id)
        return
      }
      pinRef.current = null
      const list = idsRef.current
      const edge = scroller.getBoundingClientRect().top
      const line = scroller.clientHeight / 2
      const atEnd = scroller.scrollHeight > scroller.clientHeight && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 1
      let next = list[0]
      for (const id of scroller.scrollTop > 1 ? list : []) {
        const el = document.getElementById(`sec-${id}`)
        if (!el) continue
        // A section jump leaves the head one scroll-margin below the edge; that counts as having reached it.
        const margin = parseFloat(getComputedStyle(el).scrollMarginTop) || 0
        if (el.getBoundingClientRect().top - edge <= Math.max(line, margin + 2)) next = id
      }
      setCurrent(atEnd ? list[list.length - 1] : next)
    }
    scroller.addEventListener('scroll', update, { passive: true })
    const observer = new ResizeObserver(update)
    observer.observe(scroller)
    for (const child of Array.from(scroller.children)) observer.observe(child)
    return () => {
      scroller.removeEventListener('scroll', update)
      observer.disconnect()
    }
  }, [scrollRef])

  const jump = useCallback(
    (id: T) => {
      document.getElementById(`sec-${id}`)?.scrollIntoView({ block: 'start' })
      pinRef.current = { id, top: scrollRef.current?.scrollTop ?? 0 }
      setCurrent(id)
    },
    [scrollRef],
  )

  return [current, jump] as const
}
