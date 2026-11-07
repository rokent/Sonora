// A preset: a name, its rooms (one system only) and each room's starting
// volume. Its starting music and TV show, when it has them, are kept as
// they are.
import React, { useEffect, useMemo, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useSystem } from '../lib/store.jsx'
import { homeApi } from './api.js'
import { TV_MODES, modeKey } from './tvKit.js'
import { RoomChecklist, Sheet, VolumeSlider, errorText, go, roomsBySystem } from './partyKit.jsx'

export default function PresetEditor({ data, presetId = null }) {
  const { t } = useI18n()
  const { zones, households } = useSystem()
  const systems = useMemo(() => roomsBySystem(households, zones), [households, zones])
  const preset = presetId ? (data.presets || []).find((p) => p.id === presetId) : null
  const [form, setForm] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [tvs, setTvs] = useState([])
  useEffect(() => { homeApi.tvs().then((r) => setTvs(r?.tvs || [])).catch(() => setTvs([])) }, [])

  // The form starts from the preset once the list has it.
  useEffect(() => {
    if (form) return
    if (!presetId) setForm({ name: '', speakers: [], volumes: {}, tvId: '', show: 'cover' })
    else if (preset) setForm({ name: preset.name || '', speakers: [...(preset.speakers || [])], volumes: { ...(preset.volumes || {}) }, tvId: preset.tv?.tv_id || '', show: preset.tv?.show || 'cover' })
  }, [presetId, preset, form])

  if (presetId && !preset) {
    return <p className="hp-muted">{data.loading ? t('home.loading') : t('home.error.notFound')}</p>
  }
  if (!form) return null

  function toggle(uuid, on) {
    setForm((prev) => ({
      ...prev,
      speakers: on ? [...prev.speakers, uuid] : prev.speakers.filter((u) => u !== uuid),
      volumes: on && prev.volumes[uuid] == null ? { ...prev.volumes, [uuid]: zones[uuid]?.volume ?? 20 } : prev.volumes,
    }))
  }

  async function save() {
    if (!form.name.trim() || !form.speakers.length || busy) return
    setBusy(true)
    setError('')
    const volumes = Object.fromEntries(Object.entries(form.volumes).filter(([uuid]) => form.speakers.includes(uuid)))
    const body = {
      name: form.name.trim(),
      speakers: form.speakers,
      volumes,
      ...(preset?.music ? { music: preset.music } : {}),
      ...(form.tvId ? { tv: { tv_id: form.tvId, show: form.show } } : {}),
    }
    try {
      if (preset) await homeApi.updatePreset(preset.id, body)
      else await homeApi.savePreset(body)
      data.reload?.()
      go('/party', { replace: true })
    } catch (exc) {
      setError(errorText(t, exc))
      setBusy(false)
    }
  }

  async function remove() {
    setBusy(true)
    try {
      await homeApi.deletePreset(preset.id)
      data.reload?.()
      go('/party', { replace: true })
    } catch (exc) {
      setError(errorText(t, exc))
      setBusy(false)
      setConfirmDelete(false)
    }
  }

  return (
    <section className="hp-section">
      <h1 className="hp-h1">{preset ? t('home.preset.editTitle') : t('home.preset.newTitle')}</h1>
      <label className="hp-label" htmlFor="hp-preset-name">{t('home.preset.name')}</label>
      <input id="hp-preset-name" className="hp-input" value={form.name} maxLength={60}
             onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))} />

      <h2 className="hp-h2">{t('home.start.rooms')}</h2>
      <RoomChecklist systems={systems} picked={form.speakers} onToggle={toggle} busy={busy} />

      {form.speakers.length > 0 && (
        <>
          <h2 className="hp-h2">{t('home.start.volumes')}</h2>
          <ul className="hp-list">
            {form.speakers.map((uuid) => (
              <li key={uuid} className="hp-vol">
                <span className="hp-vol__name">{zones[uuid]?.name || uuid}</span>
                <VolumeSlider
                  value={form.volumes[uuid] ?? 20}
                  label={t('home.volume.room', { room: zones[uuid]?.name || '' })}
                  every={0}
                  onSend={(level) => setForm((prev) => ({ ...prev, volumes: { ...prev.volumes, [uuid]: level } }))}
                />
                <span className="hp-vol__level">{form.volumes[uuid] ?? 20}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <h2 className="hp-h2">{t('home.preset.tv')}</h2>
      <label className="hp-label" htmlFor="hp-preset-tv">{t('home.tv.which')}</label>
      <select id="hp-preset-tv" className="hp-input" value={form.tvId}
              onChange={(e) => setForm((prev) => ({ ...prev, tvId: e.target.value }))}>
        <option value="">{t('home.preset.noTv')}</option>
        {tvs.map((tv) => <option key={tv.id} value={tv.id}>{tv.name}</option>)}
        {form.tvId && !tvs.some((tv) => tv.id === form.tvId) && <option value={form.tvId}>{form.tvId}</option>}
      </select>
      {form.tvId && (
        <>
          <label className="hp-label" htmlFor="hp-preset-show">{t('home.tv.mode')}</label>
          <select id="hp-preset-show" className="hp-input" value={form.show}
                  onChange={(e) => setForm((prev) => ({ ...prev, show: e.target.value }))}>
            {TV_MODES.map((m) => <option key={m} value={m}>{t(modeKey(m))}</option>)}
          </select>
        </>
      )}
      {preset?.music?.title && <p className="hp-muted">{t('home.preset.music', { title: preset.music.title })}</p>}

      {error && <p className="hp-bad" role="alert">{error}</p>}
      <button type="button" className="hp-btn hp-btn--primary hp-btn--wide"
              disabled={busy || !form.name.trim() || !form.speakers.length} onClick={save}>
        {t('common.save')}
      </button>
      {preset && (
        <button type="button" className="hp-btn hp-btn--danger hp-btn--wide" disabled={busy} onClick={() => setConfirmDelete(true)}>
          {t('home.preset.delete')}
        </button>
      )}
      {confirmDelete && (
        <Sheet
          title={t('home.preset.deleteTitle', { name: preset.name })}
          onClose={() => setConfirmDelete(false)}
          actions={(
            <>
              <button type="button" className="hp-btn" onClick={() => setConfirmDelete(false)}>{t('common.cancel')}</button>
              <button type="button" className="hp-btn hp-btn--danger" disabled={busy} onClick={remove}>{t('home.preset.delete')}</button>
            </>
          )}
        />
      )}
    </section>
  )
}
