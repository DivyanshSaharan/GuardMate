const OUTPUT_RATE = 16_000

/** A bounded, mono PCM16 WAV. Never relies on a browser/cloud speech service. */
export function pcmToWav(chunks: Float32Array[], inputRate: number): Blob {
  if (!Number.isFinite(inputRate) || inputRate <= 0) {
    throw new Error('The microphone returned an unsupported sample rate.')
  }
  const length = chunks.reduce((total, chunk) => total + chunk.length, 0)
  if (!length || length > inputRate * 30) {
    throw new Error('Record between a moment and 30 seconds of audio.')
  }
  const samples = new Float32Array(length)
  let offset = 0
  for (const chunk of chunks) {
    samples.set(chunk, offset)
    offset += chunk.length
  }
  const count = Math.floor((length * OUTPUT_RATE) / inputRate)
  if (!count) throw new Error('The recording was too short. Please try again.')
  const buffer = new ArrayBuffer(44 + count * 2)
  const view = new DataView(buffer)
  function ascii(at: number, value: string) {
    for (let index = 0; index < value.length; index++) {
      view.setUint8(at + index, value.charCodeAt(index))
    }
  }
  ascii(0, 'RIFF')
  view.setUint32(4, buffer.byteLength - 8, true)
  ascii(8, 'WAVE')
  ascii(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, OUTPUT_RATE, true)
  view.setUint32(28, OUTPUT_RATE * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  ascii(36, 'data')
  view.setUint32(40, count * 2, true)
  for (let index = 0; index < count; index++) {
    const start = Math.floor((index * inputRate) / OUTPUT_RATE)
    const end = Math.max(
      start + 1,
      Math.floor(((index + 1) * inputRate) / OUTPUT_RATE),
    )
    let sum = 0
    let included = 0
    for (let sample = start; sample < Math.min(end, samples.length); sample++) {
      sum += samples[sample]
      included++
    }
    const value = Math.max(-1, Math.min(1, sum / Math.max(1, included)))
    view.setInt16(44 + index * 2, value * (value < 0 ? 0x8000 : 0x7fff), true)
  }
  return new Blob([buffer], { type: 'audio/wav' })
}
