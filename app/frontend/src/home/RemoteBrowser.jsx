import React, { useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { request } from '../lib/api.js'
import './home.css'

// The household's shared browser (neko), full screen over the page. Renders
// nothing when this install has none for whoever is looking.
export default function RemoteBrowser() {
  const { t } = useI18n()
  const [url, setUrl] = useState(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let alive = true
    request('/api/home/config')
      .then((config) => { if (alive) setUrl(config?.remote_browser?.url || null) })
      .catch(() => { if (alive) setUrl(null) })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!open) return undefined
    const onKey = (event) => { if (event.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  if (!url) return null
  return (
    <>
      <button type="button" className="home-browser__open" onClick={() => setOpen(true)}>
        {t('home.browser.button')}
      </button>
      {open && (
        <div className="home-browser" role="dialog" aria-modal="true" aria-label={t('home.browser.title')}>
          <div className="home-browser__bar">
            <span className="home-browser__title">{t('home.browser.title')}</span>
            <button type="button" className="home-browser__close" onClick={() => setOpen(false)}>
              {t('home.browser.close')}
            </button>
          </div>
          <iframe className="home-browser__frame" title={t('home.browser.title')} src={url} allow="autoplay; fullscreen" />
        </div>
      )}
    </>
  )
}
