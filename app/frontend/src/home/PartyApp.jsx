// Placeholder: the party screens arrive with the next task.
import React from 'react'
import { useI18n } from '../i18n/index.jsx'

export default function PartyApp() {
  const { t } = useI18n()
  return <h1 className="home-heading">{t('home.parties.title')}</h1>
}
