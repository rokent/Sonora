// TV mode's MilkDrop visualizer. This file is small on purpose: Butterchurn
// and its presets (about a megabyte) live in VisualizerCanvas.jsx, loaded
// with React.lazy the first time a visualizer is shown, so the main bundle
// every page loads does not carry them.
//
// Props:
//   partyId          the party whose audio to follow (for /ws/visual)
//   trackUri         the track playing; a change drops queued frames
//   positionSeconds  the speaker's last reported position, seconds
//   positionAt       when that was reported, ms epoch (Date.now())
//   playing          whether the speaker is playing
//   isRelayed        whether the track's audio passes through Sonora (only
//                    then can the server analyse it)
//   offsetSeconds    optional, added to the speaker time when picking a frame
import React, { Component, Suspense, lazy } from 'react'
import { useI18n } from '../i18n/index.jsx'
import './visualizer.css'

const VisualizerCanvas = lazy(() => import('./VisualizerCanvas.jsx'))

// A chunk that fails to load (an update deployed while the TV was open, a
// dropped connection) shows the message instead of taking the page down.
class Fallback extends Component {
  constructor(props) { super(props); this.state = { failed: false } }
  static getDerivedStateFromError() { return { failed: true } }
  render() { return this.state.failed ? this.props.message : this.props.children }
}

export default function Visualizer(props) {
  const { t } = useI18n()
  return (
    <div className="home-visualizer">
      <Fallback message={<div className="home-visualizer__message" role="status">{t('home.visualizer.unsupported')}</div>}>
        <Suspense fallback={<div className="home-visualizer__message" role="status">{t('home.visualizer.loading')}</div>}>
          <VisualizerCanvas {...props} />
        </Suspense>
      </Fallback>
    </div>
  )
}
