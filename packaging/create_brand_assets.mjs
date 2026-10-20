// Regenerate committed image formats from the editable SVG. Requires sharp.
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { mkdir, writeFile } from 'node:fs/promises'

const require = createRequire(import.meta.url)
const sharp = require(process.argv[2] ? resolve(process.argv[2]) : 'sharp')
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const publicDir = join(root, 'frontend/public')
const source = join(publicDir, 'guardmate-logo.svg')
const iconDir = join(root, 'packaging/windows/assets')
await mkdir(iconDir, { recursive: true })

async function png(size) {
  return sharp(source, { density: 384 }).resize(size, size).png().toBuffer()
}

for (const [name, size] of [
  ['guardmate-logo.png', 512],
  ['apple-touch-icon.png', 180],
  ['favicon-32.png', 32],
]) {
  await writeFile(join(publicDir, name), await png(size))
}

// Windows Vista and later support PNG-compressed ICO frames up to 256 pixels.
const sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
const frames = await Promise.all(sizes.map(png))
const directory = Buffer.alloc(6 + frames.length * 16)
directory.writeUInt16LE(1, 2)
directory.writeUInt16LE(frames.length, 4)
let offset = directory.length
for (let index = 0; index < frames.length; index++) {
  const entry = 6 + index * 16
  directory[entry] = sizes[index] % 256
  directory[entry + 1] = sizes[index] % 256
  directory.writeUInt16LE(1, entry + 4)
  directory.writeUInt16LE(32, entry + 6)
  directory.writeUInt32LE(frames[index].length, entry + 8)
  directory.writeUInt32LE(offset, entry + 12)
  offset += frames[index].length
}
await writeFile(
  join(iconDir, 'guardmate.ico'),
  Buffer.concat([directory, ...frames]),
)
console.log('Generated GuardMate PNG assets and Windows icon (16–256 px).')
