import React, { useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { joinAsGuest } from './api.js'
import './home.css'

const KEY = 'sonora.guestName'

function remembered() {
  try { return localStorage.getItem(KEY) || '' } catch { return '' }
}

export default function JoinPage() {
  const { t } = useI18n()
  const token = new URLSearchParams(window.location.search).get('token') || ''
  const [name, setName] = useState(remembered)
  const [bad, setBad] = useState(!token)
  const [busy, setBusy] = useState(false)

  async function submit(event) {
    event.preventDefault()
    const clean = name.trim()
    if (!clean || busy) return
    setBusy(true)
    try {
      await joinAsGuest(token, clean)
      try { localStorage.setItem(KEY, clean) } catch { /* not remembered */ }
      window.location.assign('/party')
    } catch {
      setBad(true)
      setBusy(false)
    }
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
        {bad && <p className="home-join__bad" role="alert">{t('home.join.bad')}</p>}
        <button className="home-join__go" type="submit" disabled={busy || !name.trim()}>{t('home.join.go')}</button>
      </form>
    </main>
  )
}
