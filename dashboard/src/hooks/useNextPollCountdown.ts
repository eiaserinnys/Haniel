import { useEffect, useState } from 'react'

/** Seconds until the next poll, from last_poll + poll_interval. */
export function useNextPollCountdown(lastPoll: string | null, pollInterval: number): number | null {
  const [secs, setSecs] = useState<number | null>(null)

  useEffect(() => {
    if (!lastPoll || pollInterval <= 0) return
    const tick = () => {
      const elapsed = (Date.now() - new Date(lastPoll).getTime()) / 1000
      setSecs(Math.max(0, Math.ceil(pollInterval - elapsed)))
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [lastPoll, pollInterval])

  return lastPoll && pollInterval > 0 ? secs : null
}
