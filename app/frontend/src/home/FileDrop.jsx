// Drop music files into the party: drag them onto the box or pick them. Each
// file is uploaded on its own (one at a time, with its progress) and lands at
// the end of the party's queue. The server keeps the files only while the
// party runs.
import React, { useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { uploadPartyFile } from './api.js'
import { errorText } from './partyKit.jsx'

const MAX_BYTES = 300 * 1024 * 1024
const EXTENSIONS = ['mp3', 'm4a', 'mp4', 'm4b', 'aac', 'flac', 'wav', 'wave', 'ogg', 'oga']
const ACCEPT = `${EXTENSIONS.map((e) => `.${e}`).join(',')},audio/*`

const extension = (name) => (/\.([^.]+)$/.exec(name || '')?.[1] || '').toLowerCase()

/** The size as "12.3 MB". */
function size(bytes) {
  if (!Number.isFinite(bytes)) return ''
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

let nextKey = 0

export default function FileDrop({ party, onAdded }) {
  const { t } = useI18n()
  const [items, setItems] = useState([])
  const [over, setOver] = useState(false)
  const picker = useRef(null)
  const working = useRef(false)
  const aborter = useRef(null)
  const alive = useRef(true)
  const partyId = party.id

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; aborter.current?.abort() }
  }, [])

  const update = (key, patch) => setItems((prev) => prev.map((i) => (i.key === key ? { ...i, ...patch } : i)))

  // Upload whatever is queued, one file at a time.
  useEffect(() => {
    if (working.current) return
    const item = items.find((i) => i.state === 'queued')
    if (!item) return
    working.current = true
    const controller = new AbortController()
    aborter.current = controller
    update(item.key, { state: 'uploading', progress: 0 })
    uploadPartyFile(partyId, item.file, {
      signal: controller.signal,
      onProgress: (progress) => { if (alive.current) update(item.key, { progress }) },
    }).then((r) => {
      if (!alive.current) return
      update(item.key, { state: 'done', progress: 1, title: r?.item?.title || '', file: null })
      onAdded?.()
    }).catch((exc) => {
      if (!alive.current) return
      update(item.key, { state: 'error', error: exc?.kind === 'aborted' ? '' : errorText(t, exc), file: null })
    }).finally(() => {
      working.current = false
      aborter.current = null
      // Let the effect pick up the next queued file.
      if (alive.current) setItems((prev) => [...prev])
    })
  }, [items, partyId, onAdded, t]) // eslint-disable-line react-hooks/exhaustive-deps

  function add(fileList) {
    const added = Array.from(fileList || []).map((file) => {
      const base = { key: ++nextKey, name: file.name, size: file.size, progress: 0 }
      if (!EXTENSIONS.includes(extension(file.name))) {
        return { ...base, state: 'error', error: errorText(t, { kind: 'unsupported_type' }) }
      }
      if (file.size > MAX_BYTES) return { ...base, state: 'error', error: errorText(t, { kind: 'too_large' }) }
      if (!file.size) return { ...base, state: 'error', error: errorText(t, { kind: 'empty' }) }
      return { ...base, state: 'queued', file }
    })
    if (added.length) setItems((prev) => [...prev, ...added])
  }

  function cancel(item) {
    if (item.state === 'uploading') aborter.current?.abort()
    setItems((prev) => prev.filter((i) => i.key !== item.key))
  }

  const finished = items.some((i) => i.state === 'done' || i.state === 'error')

  return (
    <div className="hp-add">
      <div
        className={`hp-drop${over ? ' hp-drop--over' : ''}`}
        onDragEnter={(e) => { e.preventDefault(); setOver(true) }}
        onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy' }}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setOver(false) }}
        onDrop={(e) => { e.preventDefault(); setOver(false); add(e.dataTransfer?.files) }}
      >
        <p className="hp-text">{t('home.files.drop')}</p>
        <button type="button" className="hp-btn hp-btn--primary" onClick={() => picker.current?.click()}>{t('home.files.pick')}</button>
        <p className="hp-muted hp-small">{t('home.files.help')}</p>
        <input ref={picker} className="hp-sr" type="file" multiple accept={ACCEPT} tabIndex={-1} aria-hidden="true"
               onChange={(e) => { add(e.target.files); e.target.value = '' }} />
      </div>
      {items.length > 0 && (
        <ul className="hp-list">
          {items.map((item) => (
            <li key={item.key} className="hp-song">
              <span className="hp-song__text">
                <span className="hp-ellipsis hp-block">{item.state === 'done' && item.title ? item.title : item.name}</span>
                <span className={`hp-small hp-block ${item.state === 'error' ? 'hp-bad' : 'hp-muted'}`} role={item.state === 'error' ? 'alert' : undefined}>
                  {item.state === 'queued' && t('home.files.queued')}
                  {item.state === 'uploading' && t('home.files.uploading', { percent: Math.round((item.progress || 0) * 100) })}
                  {item.state === 'done' && t('home.files.added')}
                  {item.state === 'error' && (item.error || t('home.files.cancelled'))}
                  {item.state !== 'error' && ` · ${size(item.size)}`}
                </span>
                {item.state === 'uploading' && (
                  <progress className="hp-progress" max={1} value={item.progress || 0}
                            aria-label={t('home.files.uploading', { percent: Math.round((item.progress || 0) * 100) })} />
                )}
              </span>
              {(item.state === 'queued' || item.state === 'uploading') && (
                <button type="button" className="hp-btn hp-btn--small hp-btn--ghost" onClick={() => cancel(item)}>{t('common.cancel')}</button>
              )}
            </li>
          ))}
        </ul>
      )}
      {finished && (
        <button type="button" className="hp-btn hp-btn--small hp-btn--ghost"
                onClick={() => setItems((prev) => prev.filter((i) => i.state === 'queued' || i.state === 'uploading'))}>
          {t('home.files.clear')}
        </button>
      )}
    </div>
  )
}
