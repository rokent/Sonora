// Shared bits of the TV screens: the three things a TV can show.
export const TV_MODES = ['cover', 'visualizer', 'video']
export const modeKey = (mode) => `home.tv.mode.${mode}`

/** A TV outcome known on this page without the server's tv_result push: a
 * preset start whose TV was refused at once (`tv_error` in its answer). The
 * parties page shows it in the same banner as a pushed result. */
export const TV_RESULT_EVENT = 'sonora-home:tv-result'
export function announceTvResult(result) {
  try {
    window.dispatchEvent(new CustomEvent(TV_RESULT_EVENT, { detail: { ...result, at: Date.now() } }))
  } catch { /* no window: nothing to show */ }
}
