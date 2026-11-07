// Adding YouTube videos to a party: a search over YouTube and a field for a
// pasted link. The server streams the video's audio to the speakers; nothing
// is downloaded. Anyone in the party may add, guests included; there is no
// Play Next here (the server refuses it for guests).
import React, { useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { formatDuration } from '../lib/format.js'
import { homeApi } from './api.js'
import { errorText } from './partyKit.jsx'

const SEARCH_DELAY_MS = 400
const TERM_MAX = 100

function Result({ video, state, onAdd }) {
  const { t } = useI18n()
  return (
    <li className="hp-song">
      <span className="hp-song__art hp-song__art--wide">
        {video.thumbnail && <img src={video.thumbnail} alt="" loading="lazy" referrerPolicy="no-referrer" />}
      </span>
      <span className="hp-song__text">
        <span className="hp-ellipsis hp-block">{video.title}</span>
        <span className="hp-muted hp-small hp-ellipsis hp-block">
          {[video.channel, video.duration ? formatDuration(video.duration) : ''].filter(Boolean).join(' · ')}
        </span>
      </span>
      <button type="button" className="hp-btn hp-btn--small" disabled={state === 'busy' || state === 'done'}
              aria-label={`${t('home.add.add')}: ${video.title || ''}`} onClick={() => onAdd(video)}>
        {state === 'done' ? t('home.add.added') : t('home.add.add')}
      </button>
    </li>
  )
}

export default function YouTubeSearch({ party, onAdded }) {
  const { t } = useI18n()
  const [term, setTerm] = useState('')
  const [results, setResults] = useState({ term: '', loading: false, items: [], error: '' })
  const [added, setAdded] = useState({})
  const [link, setLink] = useState('')
  const [linkBusy, setLinkBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState('')

  useEffect(() => {
    const clean = term.trim().slice(0, TERM_MAX)
    if (!clean) { setResults({ term: '', loading: false, items: [], error: '' }); return undefined }
    let live = true
    setResults((prev) => ({ ...prev, loading: true }))
    const timer = setTimeout(() => {
      homeApi.youtubeSearch(clean)
        .then((r) => { if (live) setResults({ term: clean, loading: false, items: r?.results || [], error: '' }) })
        .catch((exc) => { if (live) setResults({ term: clean, loading: false, items: [], error: errorText(t, exc) }) })
    }, SEARCH_DELAY_MS)
    return () => { live = false; clearTimeout(timer) }
  }, [term, t])

  async function add(video) {
    setAdded((prev) => ({ ...prev, [video.id]: 'busy' }))
    setError('')
    setDone('')
    try {
      await homeApi.addYouTube(party.id, { video: video.id })
      setAdded((prev) => ({ ...prev, [video.id]: 'done' }))
      onAdded?.()
    } catch (exc) {
      setAdded((prev) => { const next = { ...prev }; delete next[video.id]; return next })
      setError(errorText(t, exc))
    }
  }

  async function addLink(event) {
    event.preventDefault()
    const clean = link.trim()
    if (!clean || linkBusy) return
    setLinkBusy(true)
    setError('')
    setDone('')
    try {
      const r = await homeApi.addYouTube(party.id, { video: clean })
      setLink('')
      setDone(t('home.youtube.addedTitle', { title: r?.item?.title || '' }))
      onAdded?.()
    } catch (exc) {
      setError(errorText(t, exc))
    }
    setLinkBusy(false)
  }

  return (
    <div className="hp-add">
      <form className="hp-inline" onSubmit={addLink}>
        <label className="hp-label hp-sr" htmlFor="hp-yt-link">{t('home.youtube.link')}</label>
        <input id="hp-yt-link" className="hp-input" type="text" inputMode="url" value={link} autoComplete="off"
               maxLength={300} placeholder={t('home.youtube.link')} onChange={(e) => setLink(e.target.value)} />
        <button type="submit" className="hp-btn" disabled={linkBusy || !link.trim()}>{t('home.add.add')}</button>
      </form>
      <label className="hp-label hp-sr" htmlFor="hp-yt-term">{t('home.youtube.search')}</label>
      <input id="hp-yt-term" className="hp-input" type="search" value={term} autoComplete="off" maxLength={TERM_MAX}
             placeholder={t('home.youtube.search')} onChange={(e) => setTerm(e.target.value)} />
      {error && <p className="hp-bad" role="alert">{error}</p>}
      {done && <p className="hp-muted" role="status">{done}</p>}
      {results.loading && <p className="hp-muted">{t('home.add.searching')}</p>}
      {!results.loading && results.error && <p className="hp-bad" role="alert">{results.error}</p>}
      {!results.loading && !results.error && results.term && !results.items.length && (
        <p className="hp-muted">{t('home.add.nothing', { term: results.term })}</p>
      )}
      {results.items.length > 0 && (
        <ul className="hp-list">
          {results.items.map((video) => <Result key={video.id} video={video} state={added[video.id]} onAdd={add} />)}
        </ul>
      )}
    </div>
  )
}
