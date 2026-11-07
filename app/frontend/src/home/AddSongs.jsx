// Adding songs to a party: one search box over every music service the
// party's system has, plus its Sonos Favorites. A tap puts the item at the
// end of the party's queue, credited to whoever tapped it.
//
// The services, the search and the favorites are the same calls and the same
// answers the themes' Browse pane uses, and what can be queued is decided by
// the same rules (lib/items.js canEnqueue for a service's results, Browse's
// isQueueable for a favorite), so a station never lands in a queue here
// either.
//
// A second tab adds YouTube videos (YouTubeSearch.jsx).
import React, { useEffect, useMemo, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { api } from '../lib/api.js'
import { canEnqueue } from '../lib/items.js'
import { isQueueable } from '../parts/Browse.jsx'
import { TUNEIN } from '../lib/sonosServices.js'
import Art from '../components/Art.jsx'
import { homeApi } from './api.js'
import { errorText } from './partyKit.jsx'
import YouTubeSearch from './YouTubeSearch.jsx'

const SEARCH_DELAY_MS = 450

/** A favorite as something to enqueue: a shortcut carries its target. */
function favoritePick(item) {
  return item.uri ? item : { ...item, uri: item.favorite_uri || '', metadata: item.favorite_metadata || '' }
}

function Row({ item, state, onAdd }) {
  const { t } = useI18n()
  return (
    <li className="hp-song">
      <span className="hp-song__art"><Art src={item.art} size={40} fallback="track" /></span>
      <span className="hp-song__text">
        <span className="hp-ellipsis hp-block">{item.title}</span>
        {(item.artist || item.album) && <span className="hp-muted hp-small hp-ellipsis hp-block">{item.artist || item.album}</span>}
      </span>
      <button type="button" className="hp-btn hp-btn--small" disabled={state === 'busy' || state === 'done'} onClick={() => onAdd(item)}>
        {state === 'done' ? t('home.add.added') : t('home.add.add')}
      </button>
    </li>
  )
}

/** Where songs come from: the system's music services, or YouTube. */
export default function AddSongs({ party, onAdded }) {
  const { t } = useI18n()
  const [source, setSource] = useState('music')
  return (
    <>
      <div className="hp-tabs hp-tabs--sub" role="tablist">
        <button type="button" role="tab" aria-selected={source === 'music'} className="hp-tab"
                onClick={() => setSource('music')}>{t('home.add.music')}</button>
        <button type="button" role="tab" aria-selected={source === 'youtube'} className="hp-tab"
                onClick={() => setSource('youtube')}>{t('home.add.youtube')}</button>
      </div>
      {source === 'music' ? <MusicSearch party={party} onAdded={onAdded} /> : <YouTubeSearch party={party} onAdded={onAdded} />}
    </>
  )
}

function MusicSearch({ party, onAdded }) {
  const { t } = useI18n()
  const lead = party.speakers?.[0] || ''
  const [term, setTerm] = useState('')
  const [services, setServices] = useState([])
  const [favorites, setFavorites] = useState([])
  const [results, setResults] = useState({ term: '', loading: false, sections: [] })
  const [added, setAdded] = useState({})
  const [error, setError] = useState('')

  // The party's system's services, with TuneIn on an S1 system as Browse
  // lists it (it is built in there and searchable with no account).
  useEffect(() => {
    let live = true
    api.services().then((r) => {
      if (!live) return
      const hh = (r?.households || []).find((h) => h.household === party.household)
      const list = [...(hh?.in_use || [])]
      const tunein = hh?.generation === 'S1' ? (hh.available || []).find((s) => s.id === TUNEIN) : null
      if (tunein && !list.some((s) => s.id === TUNEIN)) list.push(tunein)
      setServices(list)
    }).catch(() => { if (live) setServices([]) })
    return () => { live = false }
  }, [party.household])

  useEffect(() => {
    if (!lead) return undefined
    let live = true
    api.favorites(lead)
      .then((r) => { if (live) setFavorites((r?.items || []).map(favoritePick).filter(isQueueable)) })
      .catch(() => { if (live) setFavorites([]) })
    return () => { live = false }
  }, [lead])

  useEffect(() => {
    const clean = term.trim()
    if (!clean || !lead) { setResults({ term: '', loading: false, sections: [] }); return undefined }
    let live = true
    setResults((prev) => ({ ...prev, loading: true }))
    const timer = setTimeout(async () => {
      const answers = await Promise.all(services.map((svc) =>
        api.searchService(svc.id, { zone: lead, term: clean, count: 10 })
          .then((r) => ({
            svc,
            items: r?.error ? [] : (r?.categories || []).flatMap((cat) => cat.items || []).filter(canEnqueue),
          }))
          .catch(() => ({ svc, items: [] }))))
      if (!live) return
      const seen = new Set()
      const sections = answers.map(({ svc, items }) => ({
        key: String(svc.id),
        name: svc.name,
        items: items.filter((i) => (seen.has(i.uri) ? false : seen.add(i.uri))),
      })).filter((s) => s.items.length)
      setResults({ term: clean, loading: false, sections })
    }, SEARCH_DELAY_MS)
    return () => { live = false; clearTimeout(timer) }
  }, [term, services, lead])

  const favoriteHits = useMemo(() => {
    const clean = term.trim().toLowerCase()
    return clean ? favorites.filter((f) => (f.title || '').toLowerCase().includes(clean)) : favorites
  }, [favorites, term])

  async function add(item) {
    const key = item.uri
    setAdded((prev) => ({ ...prev, [key]: 'busy' }))
    setError('')
    try {
      await homeApi.enqueue(party.id, { uri: item.uri, metadata: item.metadata || '', title: item.title || '' })
      setAdded((prev) => ({ ...prev, [key]: 'done' }))
      onAdded?.()
    } catch (exc) {
      setAdded((prev) => { const next = { ...prev }; delete next[key]; return next })
      setError(errorText(t, exc))
    }
  }

  return (
    <div className="hp-add">
      <label className="hp-label hp-sr" htmlFor="hp-add-term">{t('home.add.search')}</label>
      <input id="hp-add-term" className="hp-input" type="search" value={term} autoComplete="off"
             placeholder={t('home.add.search')} onChange={(e) => setTerm(e.target.value)} />
      {error && <p className="hp-bad" role="alert">{error}</p>}
      {results.loading && <p className="hp-muted">{t('home.add.searching')}</p>}
      {!results.loading && results.term && !results.sections.length && !favoriteHits.length && (
        <p className="hp-muted">{t('home.add.nothing', { term: results.term })}</p>
      )}
      {results.sections.map((section) => (
        <section key={section.key} className="hp-add__section">
          <h3 className="hp-h3">{section.name}</h3>
          <ul className="hp-list">
            {section.items.map((item) => <Row key={`${section.key}:${item.uri}`} item={item} state={added[item.uri]} onAdd={add} />)}
          </ul>
        </section>
      ))}
      {favoriteHits.length > 0 && (
        <section className="hp-add__section">
          <h3 className="hp-h3">{t('home.add.favorites')}</h3>
          <ul className="hp-list">
            {favoriteHits.map((item) => <Row key={`fav:${item.id || item.uri}`} item={item} state={added[item.uri]} onAdd={add} />)}
          </ul>
        </section>
      )}
    </div>
  )
}
