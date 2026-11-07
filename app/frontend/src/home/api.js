// The household features: parties, presets, the guest pass, Home Assistant
// tokens, and who is signed in. Thin wrappers over the shared request helper.
import { request } from '../lib/api.js'
import { withBase } from '../lib/base.js'

const id = (value) => encodeURIComponent(value)
const post = (path, body) => request(path, { method: 'POST', body: body ?? {} })

export const homeApi = {
  me: () => request('/api/me'),
  parties: () => request('/api/parties'),
  startParty: (body) => post('/api/parties', body),
  endParty: (pid) => post(`/api/parties/${id(pid)}/end`),
  addSpeaker: (pid, body) => post(`/api/parties/${id(pid)}/speakers`, body),
  removeSpeaker: (pid, zone) => request(`/api/parties/${id(pid)}/speakers/${id(zone)}`, { method: 'DELETE' }),
  joinParty: (pid) => post(`/api/parties/${id(pid)}/join`),
  partyQueue: (pid) => request(`/api/parties/${id(pid)}/queue`),
  enqueue: (pid, body) => post(`/api/parties/${id(pid)}/queue`, body),
  dequeue: (pid, index) => request(`/api/parties/${id(pid)}/queue/${id(index)}`, { method: 'DELETE' }),
  transport: (pid, action) => post(`/api/parties/${id(pid)}/transport/${id(action)}`),
  volume: (pid, body) => post(`/api/parties/${id(pid)}/volume`, body),
  youtubeSearch: (q) => request(`/api/youtube/search?q=${id(q)}`),
  addYouTube: (pid, body) => post(`/api/parties/${id(pid)}/youtube`, body),
  presets: () => request('/api/presets'),
  savePreset: (body) => post('/api/presets', body),
  updatePreset: (pid, body) => request(`/api/presets/${id(pid)}`, { method: 'PUT', body }),
  deletePreset: (pid) => request(`/api/presets/${id(pid)}`, { method: 'DELETE' }),
  startPreset: (pid, body) => post(`/api/presets/${id(pid)}/start`, body),
  guestPass: () => request('/api/guest-pass'),
  rotateGuestPass: () => post('/api/guest-pass/rotate'),
  haTokens: () => request('/api/ha/tokens'),
  createHaToken: (label) => post('/api/ha/tokens', { label }),
  tvs: () => request('/api/tvs'),
  addTv: (body) => post('/api/tvs', body),
  deleteTv: (tid) => request(`/api/tvs/${id(tid)}`, { method: 'DELETE' }),
  pairTv: (tid) => post(`/api/tvs/${id(tid)}/pair`),
  showOnTv: (tid, body) => post(`/api/tvs/${id(tid)}/show`, body),
  displays: () => request('/api/displays'),
  // A display reads its own link; a household member names one (?display=).
  displayMe: (did = '') => request(`/api/displays/me${did ? `?display=${id(did)}` : ''}`),
  createDisplay: (body) => post('/api/displays', body),
  updateDisplay: (did, body) => request(`/api/displays/${id(did)}`, { method: 'PUT', body }),
  deleteDisplay: (did) => request(`/api/displays/${id(did)}`, { method: 'DELETE' }),
  deleteHaToken: (tid) => request(`/api/ha/tokens/${id(tid)}`, { method: 'DELETE' }),
  screamSources: () => request('/api/scream/sources'),
  mapScream: (ip, target) => request(`/api/scream/sources/${id(ip)}`, { method: 'PUT', body: { target } }),
  airplayReceivers: () => request('/api/airplay/receivers'),
}

/**
 * Upload one music file into a party's queue: the body is the file itself.
 * Resolves with the server's answer ({ok, item}); rejects with
 * {status, kind, body} (kind is the server's error word, 'network' or
 * 'aborted'), which errorText understands. `onProgress(0..1)` follows the
 * upload; `signal` aborts it.
 */
export function uploadPartyFile(pid, file, { next = false, onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const query = `name=${id(file.name || '')}${next ? '&next=true' : ''}`
    xhr.open('POST', withBase(`/api/parties/${id(pid)}/files?${query}`))
    xhr.responseType = 'text'
    const fail = (status, kind, body = null) => reject({ status, kind, body: body || { error: kind } })
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress?.(event.loaded / event.total)
    }
    xhr.onload = () => {
      let body = null
      try { body = xhr.responseText ? JSON.parse(xhr.responseText) : null } catch { body = null }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body)
      else fail(xhr.status, body?.error || `http_${xhr.status}`, body)
    }
    xhr.onerror = () => fail(0, 'network')
    xhr.onabort = () => fail(0, 'aborted')
    if (signal) {
      if (signal.aborted) { fail(0, 'aborted'); return }
      signal.addEventListener('abort', () => xhr.abort(), { once: true })
    }
    xhr.send(file)
  })
}

/** Sign the guest in with the pass token and a first name. */
export const joinAsGuest = (token, name) => post('/auth/guest', { token, name })
