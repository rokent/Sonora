import { useEffect, useState } from 'react'
import { homeApi } from './api.js'

// Who is looking, fetched once per page load and shared.
let cached = null
let pending = null

function load() {
  if (!pending) {
    pending = homeApi.me()
      .then((me) => { cached = { auth: !!me?.auth, principal: me?.principal || null } })
      .catch(() => { cached = { auth: false, principal: null } })
  }
  return pending
}

/** -> {loading, auth, principal: {kind, id, name} | null} */
export function useMe() {
  const [me, setMe] = useState(cached)
  useEffect(() => {
    if (cached) return undefined
    let live = true
    load().then(() => { if (live) setMe(cached) })
    return () => { live = false }
  }, [])
  return me ? { loading: false, ...me } : { loading: true, auth: false, principal: null }
}
