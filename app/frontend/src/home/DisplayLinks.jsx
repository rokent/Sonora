// Display links: addresses that open the TV screen on any device (a TV's
// browser, a tablet on the wall) without signing in. The link is shown once,
// when it is made.
import React, { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { homeApi } from './api.js'
import { Sheet, errorText } from './partyKit.jsx'
import { TV_MODES, modeKey } from './tvKit.js'

export default function DisplayLinks({ data }) {
  const { t } = useI18n()
  const [links, setLinks] = useState(null)
  const [form, setForm] = useState({ name: '', party: '', mode: 'visualizer', lyrics: false })
  const [created, setCreated] = useState(null)   // { name, url, qr }
  const [copied, setCopied] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [removing, setRemoving] = useState(null)
  const parties = data?.parties || []

  const load = useCallback(() => homeApi.displays()
    .then((r) => setLinks(r?.displays || []))
    .catch((exc) => { setLinks([]); setError(errorText(t, exc)) }), [t])
  useEffect(() => { load() }, [load])

  async function create(event) {
    event.preventDefault()
    if (!form.name.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      const r = await homeApi.createDisplay({
        name: form.name.trim(), party: form.party || null, mode: form.mode, lyrics: form.lyrics,
      })
      let qr = ''
      try {
        // Loaded here, so the QR library is fetched only when a link is made.
        const { default: QRCode } = await import('qrcode')
        qr = await QRCode.toDataURL(r.url, { margin: 1, width: 512, errorCorrectionLevel: 'M' })
      } catch {
        qr = ''
      }
      setCreated({ name: form.name.trim(), url: r.url, qr })
      setCopied(false)
      setForm({ name: '', party: '', mode: 'visualizer', lyrics: false })
      load()
    } catch (exc) {
      setError(errorText(t, exc))
    }
    setBusy(false)
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(created.url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)   // plain-HTTP page: the link is selectable
    }
  }

  async function remove() {
    const link = removing
    setRemoving(null)
    try {
      await homeApi.deleteDisplay(link.id)
    } catch (exc) {
      setError(errorText(t, exc))
    }
    load()
  }

  const partyName = (id) => (id ? (parties.find((p) => p.id === id)?.name || id) : t('home.display.anyParty'))

  return (
    <section className="hp-section">
      <h1 className="hp-h1">{t('home.display.title')}</h1>
      <p className="hp-muted">{t('home.display.help')}</p>
      {links && !links.length && <p className="hp-muted">{t('home.display.none')}</p>}
      <ul className="hp-list">
        {(links || []).map((link) => (
          <li key={link.id} className="hp-card hp-row">
            <div className="hp-stack">
              <div className="hp-party__name">{link.name}</div>
              <div className="hp-muted hp-small">
                {partyName(link.party)} · {t(modeKey(link.mode))}{link.lyrics ? ` · ${t('lyrics.show')}` : ''}
              </div>
            </div>
            <button type="button" className="hp-btn hp-btn--small hp-btn--danger" onClick={() => setRemoving(link)}>
              {t('home.display.delete')}
            </button>
          </li>
        ))}
      </ul>
      {error && <p className="hp-bad" role="alert">{error}</p>}

      <h2 className="hp-h2">{t('home.display.new')}</h2>
      <form className="hp-stack" onSubmit={create}>
        <label className="hp-label" htmlFor="hp-dl-name">{t('home.display.name')}</label>
        <input id="hp-dl-name" className="hp-input" maxLength={40} value={form.name}
               onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))} />
        <label className="hp-label" htmlFor="hp-dl-party">{t('home.display.party')}</label>
        <select id="hp-dl-party" className="hp-input" value={form.party}
                onChange={(e) => setForm((prev) => ({ ...prev, party: e.target.value }))}>
          <option value="">{t('home.display.anyParty')}</option>
          {parties.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <label className="hp-label" htmlFor="hp-dl-mode">{t('home.tv.mode')}</label>
        <select id="hp-dl-mode" className="hp-input" value={form.mode}
                onChange={(e) => setForm((prev) => ({ ...prev, mode: e.target.value }))}>
          {TV_MODES.map((m) => <option key={m} value={m}>{t(modeKey(m))}</option>)}
        </select>
        <label className="hp-check">
          <input type="checkbox" checked={form.lyrics}
                 onChange={(e) => setForm((prev) => ({ ...prev, lyrics: e.target.checked }))} />
          <span className="hp-check__name">{t('lyrics.show')}</span>
        </label>
        <p className="hp-muted hp-small">{t('lyrics.notice')}</p>
        <button type="submit" className="hp-btn hp-btn--primary hp-btn--wide" disabled={busy || !form.name.trim()}>
          {t('home.display.create')}
        </button>
      </form>

      {created && (
        <Sheet
          title={t('home.display.createdTitle', { name: created.name })}
          onClose={() => setCreated(null)}
          actions={(
            <>
              <button type="button" className="hp-btn" onClick={() => setCreated(null)}>{t('common.close')}</button>
              <button type="button" className="hp-btn hp-btn--primary" onClick={copy}>
                {copied ? t('home.guest.copied') : t('home.guest.copy')}
              </button>
            </>
          )}
        >
          <p className="hp-text">{t('home.display.createdHelp')}</p>
          {created.qr && <img className="hp-qr" src={created.qr} alt={t('home.display.qrAlt')} width={256} height={256} />}
          <p className="hp-link"><span className="hp-select">{created.url}</span></p>
        </Sheet>
      )}
      {removing && (
        <Sheet
          title={t('home.display.deleteTitle', { name: removing.name })}
          onClose={() => setRemoving(null)}
          actions={(
            <>
              <button type="button" className="hp-btn" onClick={() => setRemoving(null)}>{t('common.cancel')}</button>
              <button type="button" className="hp-btn hp-btn--danger" onClick={remove}>{t('home.display.delete')}</button>
            </>
          )}
        >
          <p className="hp-text">{t('home.display.deleteHelp')}</p>
        </Sheet>
      )}
    </section>
  )
}
