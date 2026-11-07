// The household features: parties, presets, the guest pass, Home Assistant
// tokens, and who is signed in. Thin wrappers over the shared request helper.
import { request } from '../lib/api.js'

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
  createDisplay: (body) => post('/api/displays', body),
  updateDisplay: (did, body) => request(`/api/displays/${id(did)}`, { method: 'PUT', body }),
  deleteDisplay: (did) => request(`/api/displays/${id(did)}`, { method: 'DELETE' }),
  deleteHaToken: (tid) => request(`/api/ha/tokens/${id(tid)}`, { method: 'DELETE' }),
}

/** Sign the guest in with the pass token and a first name. */
export const joinAsGuest = (token, name) => post('/auth/guest', { token, name })
