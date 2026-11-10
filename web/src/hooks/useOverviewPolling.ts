import { useEffect, useRef } from 'react'
import { APIError, api } from '../api'
import type { Overview } from '../types'

/** One request at a time; timers and late results cannot outlive the view. */
export function useOverviewPolling(enabled: boolean, onValue: (value: Overview) => void,
  onUnauthorized: () => void, onStale: (value: boolean) => void) {
  const callbacks = useRef({ onValue, onUnauthorized, onStale })
  callbacks.current = { onValue, onUnauthorized, onStale }
  useEffect(() => {
    if (!enabled) return
    let stopped = false
    let failures = 0
    let timer: number | undefined
    const poll = async () => {
      try {
        const next = await api<Overview>('/api/v1/overview')
        if (stopped) return
        callbacks.current.onValue(next)
        failures = 0
        callbacks.current.onStale(false)
      } catch (error) {
        if (stopped) return
        if (error instanceof APIError && error.status === 401) callbacks.current.onUnauthorized()
        else if (++failures >= 2) callbacks.current.onStale(true)
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 10_000)
      }
    }
    timer = window.setTimeout(poll, 10_000)
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [enabled])
}
