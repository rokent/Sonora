import React, { useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { joinAsGuest } from './api.js'
import { withBase } from '../lib/base.js'
import { useMe } from './useMe.js'
import { GUEST_NAME_KEY, joinScreen, loginTarget } from './access.js'
import './home.css'

function remembered() {
  try { return localStorage.getItem(GUEST_NAME_KEY) || '' } catch { return '' }
}

export default function JoinPage() {
  const { t } = useI18n()
  const me = useMe()
  const params = new URLSearchParams(window.location.search)
  const token = params.get('token') || ''
  // Sent here by the server from the party page, with no session behind it.
  const expired = params.get('expired') === '1'
  const next = params.get('next')
  const [name, setName] = useState(remembered)
  const [bad, setBad] = useState('')
  const [busy, setBusy] = useState(false)
  const screen = me.loading ? null : joinScreen({
    token, expired, principalKind: me.principal?.kind || null, rememberedName: remembered(),
  })

  useEffect(() => {
    if (screen === 'login') window.location.replace(withBase(loginTarget(next)))
  }, [screen, next])

  async function submit(event) {
    event.preventDefault()
    const clean = name.trim()
    if (!clean || busy) return
    setBusy(true)
    try {
      await joinAsGuest(token, clean)
      try { localStorage.setItem(GUEST_NAME_KEY, clean) } catch { /* not remembered */ }
      window.location.assign(withBase('/party'))
    } catch (error) {
      setBad(error?.status === 400 ? 'name' : error?.status === 403 ? 'link' : 'network')
      setBusy(false)
    }
  }

  if (screen === null || screen === 'login') {
    return (
      <main className="home-join">
        <div className="home-join__card">
          {screen === 'login' && <p className="home-join__label" role="status">{t('home.join.signingIn')}</p>}
        </div>
      </main>
    )
  }

  if (screen === 'signedIn') {
    return (
      <main className="home-join">
        <div className="home-join__card">
          <h1 className="home-join__title">{t('home.join.title')}</h1>
          <a className="home-join__go" href={withBase('/party')}>{t('home.join.signedIn')}</a>
        </div>
      </main>
    )
  }

  if (screen === 'expired' || screen === 'badLink') {
    return (
      <main className="home-join">
        <div className="home-join__card">
          <h1 className="home-join__title">{t('home.join.title')}</h1>
          <p className="home-join__bad" role="alert">{t(screen === 'expired' ? 'home.join.expired' : 'home.join.bad')}</p>
          {screen === 'expired' && (
            <a className="home-join__label" href={withBase(loginTarget(next))}>{t('home.join.member')}</a>
          )}
        </div>
      </main>
    )
  }

  return (
    <main className="home-join">
      <form className="home-join__card" onSubmit={submit}>
        <h1 className="home-join__title">{t('home.join.title')}</h1>
        <label className="home-join__label" htmlFor="home-join-name">{t('home.join.name')}</label>
        <input
          id="home-join-name"
          className="home-join__input"
          value={name}
          maxLength={40}
          autoComplete="given-name"
          onChange={(e) => setName(e.target.value)}
        />
        {bad && <p className="home-join__bad" role="alert">{t(bad === 'name' ? 'home.join.badName' : bad === 'network' ? 'home.join.network' : 'home.join.bad')}</p>}
        <button className="home-join__go" type="submit" disabled={busy || !token || !name.trim()}>{t('home.join.go')}</button>
      </form>
    </main>
  )
}
