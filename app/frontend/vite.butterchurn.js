// Butterchurn's presets, compiled when the page is built rather than in the
// browser.
//
// A butterchurn-presets preset carries its MilkDrop equations as JavaScript
// source strings (`init_eqs_str`, `frame_eqs_str`, ...), and Butterchurn's
// loadPreset() turns them into functions with `new Function` -- which the
// page's Content-Security-Policy (`script-src 'self'`, no 'unsafe-eval')
// forbids. loadPreset() skips that step when a preset already carries
// `init_eqs` as a function, so this plugin writes every equation out as a
// real function in an ordinary module, `virtual:butterchurn-presets`, which
// the bundler then treats like any other code. The package itself is only
// read here, at build time; none of it ships to the page.
//
// Each body is first compiled on its own, in strict mode, so one that does
// not parse as a complete function body (and so could not be embedded
// safely) fails the build instead of reaching the page.
import { createRequire } from 'node:module'

const ID = 'virtual:butterchurn-presets'
const RESOLVED = `\0${ID}`
const require = createRequire(import.meta.url)

function fn(body, where) {
  const text = String(body ?? '')
  try {
    // eslint-disable-next-line no-new-func -- build time, in node: a parse check
    new Function('a', `"use strict";${text}\nreturn a;`)
  } catch (error) {
    throw new Error(`butterchurn preset ${where}: ${error.message}`)
  }
  return `function (a) {\n${text}\nreturn a;\n}`
}

// The same fields loadPreset() would have built, in the same shapes: an empty
// pixel or point equation is '' (the renderer tests for that), everything
// else a function. The *_str source is dropped.
function presetSource(name, preset) {
  const strip = (obj) => Object.fromEntries(Object.entries(obj).filter(([k]) => !k.endsWith('_eqs_str')))
  const where = (part) => `${JSON.stringify(name)} ${part}`
  const { shapes: _s, waves: _w, ...rest } = preset
  const fields = [`...${JSON.stringify(strip(rest))}`]
  fields.push(`init_eqs: ${fn(preset.init_eqs_str, where('init'))}`)
  fields.push(`frame_eqs: ${fn(preset.frame_eqs_str, where('frame'))}`)
  fields.push(`pixel_eqs: ${preset.pixel_eqs_str ? fn(preset.pixel_eqs_str, where('pixel')) : "''"}`)
  const shapes = (preset.shapes || []).map((shape, i) => `{...${JSON.stringify(strip(shape))}, `
    + `init_eqs: ${fn(shape.init_eqs_str, where(`shape ${i} init`))}, `
    + `frame_eqs: ${fn(shape.frame_eqs_str, where(`shape ${i} frame`))}}`)
  const waves = (preset.waves || []).map((wave, i) => `{...${JSON.stringify(strip(wave))}, `
    + `init_eqs: ${fn(wave.init_eqs_str, where(`wave ${i} init`))}, `
    + `frame_eqs: ${fn(wave.frame_eqs_str, where(`wave ${i} frame`))}, `
    + `point_eqs: ${wave.point_eqs_str ? fn(wave.point_eqs_str, where(`wave ${i} point`)) : "''"}}`)
  fields.push(`shapes: [${shapes.join(',\n')}]`)
  fields.push(`waves: [${waves.join(',\n')}]`)
  return `${JSON.stringify(name)}: {${fields.join(',\n')}}`
}

export function butterchurnPresets() {
  return {
    name: 'sonora-butterchurn-presets',
    resolveId(id) { return id === ID ? RESOLVED : null },
    load(id) {
      if (id !== RESOLVED) return null
      const pack = require('butterchurn-presets')
      const presets = (pack.getPresets || pack.default.getPresets)()
      const entries = Object.entries(presets).map(([name, preset]) => presetSource(name, preset))
      return `// Generated from butterchurn-presets by vite.butterchurn.js.\n`
        + `export default {\n${entries.join(',\n')}\n}\n`
    },
  }
}
