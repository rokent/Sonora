// The guest pass: a QR code and a link that sign a guest in, and a way to
// make a new one, which stops the old one working.
import React, { useEffect, useState } from 'react'
import QRCode from 'qrcode'
import { useI18n } from '../i18n/index.jsx'
import { withBase } from '../lib/base.js'
import { homeApi } from './api.js'
import { Sheet, errorText } from './partyKit.jsx'

/** The join link on the address this page was opened on, so a guest's phone
 * reaches the same Sonora the host is looking at. */
function joinUrl(token) {
  return `${window.location.origin}${withBase('/join')}?token=${encodeURIComponent(token)}`
}

export default function GuestPass() {
  const { t } = useI18n()
  const [token, setToken] = useState('')
  const [qr, setQr] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [asking, setAsking] = useState(false)

  useEffect(() => {
    let live = true
    homeApi.guestPass()
      .then((r) => { if (live) setToken(r?.token || '') })
      .catch((exc) => { if (live) setError(errorText(t, exc)) })
    return () => { live = false }
  }, [t])

  const url = token ? joinUrl(token) : ''
  useEffect(() => {
    if (!url) { setQr(''); return undefined }
    let live = true
    QRCode.toDataURL(url, { margin: 1, width: 512, errorCorrectionLevel: 'M' })
      .then((data) => { if (live) setQr(data) })
      .catch(() => { if (live) setQr('') })
    return () => { live = false }
  }, [url])

  async function copy() {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // No clipboard on a plain-HTTP page: the link is selectable below.
      setCopied(false)
    }
  }

  async function rotate() {
    setAsking(false)
    setBusy(true)
    setError('')
    try {
      const r = await homeApi.rotateGuestPass()
      setToken(r?.token || '')
    } catch (exc) {
      setError(errorText(t, exc))
    }
    setBusy(false)
  }

  return (
    <section className="hp-section hp-guest">
      <h1 className="hp-h1">{t('home.guest.title')}</h1>
      <p className="hp-muted">{t('home.guest.help')}</p>
      {qr && <img className="hp-qr" src={qr} alt={t('home.guest.qrAlt')} width={256} height={256} />}
      {url && <p className="hp-link"><span className="hp-select">{url}</span></p>}
      {error && <p className="hp-bad" role="alert">{error}</p>}
      <div className="hp-actions">
        <button type="button" className="hp-btn hp-btn--primary hp-btn--wide" disabled={!url} onClick={copy}>
          {copied ? t('home.guest.copied') : t('home.guest.copy')}
        </button>
        <button type="button" className="hp-btn hp-btn--wide" disabled={busy} onClick={() => setAsking(true)}>
          {t('home.guest.rotate')}
        </button>
      </div>
      {asking && (
        <Sheet
          title={t('home.guest.rotateTitle')}
          onClose={() => setAsking(false)}
          actions={(
            <>
              <button type="button" className="hp-btn" onClick={() => setAsking(false)}>{t('common.cancel')}</button>
              <button type="button" className="hp-btn hp-btn--danger" onClick={rotate}>{t('home.guest.rotate')}</button>
            </>
          )}
        >
          <p className="hp-text">{t('home.guest.rotateHelp')}</p>
        </Sheet>
      )}
    </section>
  )
}
