import { describe, expect, it } from 'vitest'
import { pcmToWav } from './pcm'

function readBlob(blob: Blob): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as ArrayBuffer)
    reader.onerror = reject
    reader.readAsArrayBuffer(blob)
  })
}

describe('bounded local PCM encoder', () => {
  it('downsamples microphone chunks into mono 16 kHz PCM16 WAV', async () => {
    const wav = pcmToWav([new Float32Array(48_000).fill(0.5)], 48_000)
    const view = new DataView(await readBlob(wav))
    expect(wav.type).toBe('audio/wav')
    expect(wav.size).toBe(44 + 16_000 * 2)
    expect(view.getUint16(20, true)).toBe(1)
    expect(view.getUint16(22, true)).toBe(1)
    expect(view.getUint32(24, true)).toBe(16_000)
    expect(view.getUint16(34, true)).toBe(16)
    expect(view.getInt16(44, true)).toBe(16_383)
  })

  it('clips over-range samples and preserves both chunk boundaries', async () => {
    const wav = pcmToWav(
      [new Float32Array([2]), new Float32Array([-2])],
      16_000,
    )
    const view = new DataView(await readBlob(wav))
    expect(view.getInt16(44, true)).toBe(32_767)
    expect(view.getInt16(46, true)).toBe(-32_768)
  })

  it.each([0, -1, Number.NaN])('rejects invalid input rate %s', (rate) => {
    expect(() => pcmToWav([new Float32Array(100)], rate)).toThrow('sample rate')
  })

  it('rejects empty and longer-than-30-second captures', () => {
    expect(() => pcmToWav([], 16_000)).toThrow('30 seconds')
    expect(() => pcmToWav([new Float32Array(480_001)], 16_000)).toThrow(
      '30 seconds',
    )
  })
})
