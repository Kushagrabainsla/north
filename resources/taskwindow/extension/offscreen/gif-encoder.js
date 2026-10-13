/**
 * Minimal animated GIF89a encoder.
 *
 * Frames are RGBA pixel buffers; colors are mapped to the fixed web-safe
 * 216-color palette (6x6x6 cube) via an RGB555 lookup table, which keeps the
 * encoder deterministic and small. Quality ceiling: banding on gradients —
 * swap for median-cut quantization if that ever matters.
 *
 * The LZW width-switch rules follow omggif (the reference implementation this
 * was checked against): the encoder widens the code size BEFORE assigning an
 * entry that wouldn't fit, and emits a CLEAR when the table is full; the
 * decoder widens after adding the entry one behind, and stops adding at 4096.
 */

// LUT: 5-bit-per-channel RGB (32768 entries) -> web-safe palette index (0..215).
// Web-safe levels are k*255/5, so a 5-bit sample s (~= level*31/255) maps to
// round(s*5/31) — exact (bijective) for the web-safe colors themselves.
const LUT = new Uint8Array(32768);
for (let r = 0; r < 32; r++) {
  for (let g = 0; g < 32; g++) {
    for (let b = 0; b < 32; b++) {
      LUT[(r << 10) | (g << 5) | b] =
        Math.round((r * 5) / 31) * 36 + Math.round((g * 5) / 31) * 6 + Math.round((b * 5) / 31);
    }
  }
}

// 256-entry local color table: 216 web-safe colors, remainder black.
const PALETTE = new Uint8Array(256 * 3);
for (let i = 0; i < 216; i++) {
  PALETTE[i * 3 + 0] = Math.floor(i / 36) * 51;
  PALETTE[i * 3 + 1] = (Math.floor(i / 6) % 6) * 51;
  PALETTE[i * 3 + 2] = (i % 6) * 51;
}

/**
 * @param {Array<{rgba: Uint8ClampedArray|Uint8Array, width: number, height: number}>} frames
 * @param {{delayMs?: number}} opts
 * @returns {Uint8Array} complete GIF file bytes
 */
export function encodeGif(frames, { delayMs = 100 } = {}) {
  if (!frames?.length) throw new Error("encodeGif: no frames");
  const { width, height } = frames[0];
  const delayCs = Math.max(2, Math.round(delayMs / 10)); // centiseconds, min 20ms

  const out = [];
  const push = (...bytes) => out.push(...bytes);
  const pushStr = (s) => {
    for (let i = 0; i < s.length; i++) out.push(s.charCodeAt(i));
  };
  const pushU16 = (v) => push(v & 0xff, (v >> 8) & 0xff);

  pushStr("GIF89a");
  // Logical screen descriptor: no global color table (each frame carries a local one).
  pushU16(width);
  pushU16(height);
  push(0x70, 0x00, 0x00);

  for (const frame of frames) {
    if (frame.width !== width || frame.height !== height) {
      throw new Error("encodeGif: all frames must share dimensions");
    }

    // Graphic Control Extension: disposal "do not dispose" so partial updates
    // from lossy frame sources never bleed through.
    push(0x21, 0xf9, 0x04, 0x04, delayCs & 0xff, (delayCs >> 8) & 0xff, 0x00, 0x00);

    // Image descriptor + local color table (256 entries, 8-bit LZW minimum).
    push(0x2c);
    pushU16(0);
    pushU16(0);
    pushU16(width);
    pushU16(height);
    push(0x87);
    out.push(...PALETTE);

    // Index pixels.
    const n = width * height;
    const indices = new Uint8Array(n);
    const rgba = frame.rgba;
    for (let i = 0; i < n; i++) {
      const o = i * 4;
      indices[i] = LUT[((rgba[o] >> 3) << 10) | ((rgba[o + 1] >> 3) << 5) | (rgba[o + 2] >> 3)];
    }

    push(0x08); // LZW minimum code size (palette is 256 entries)
    const lzw = lzwEncode(indices);
    for (let i = 0; i < lzw.length; i += 255) {
      const chunk = lzw.subarray(i, Math.min(i + 255, lzw.length));
      push(chunk.length);
      out.push(...chunk);
    }
    push(0x00); // block terminator
  }

  push(0x3b); // trailer
  return new Uint8Array(out);
}

const CLEAR_CODE = 256;
const EOI_CODE = 257;

/** Standard GIF-flavor LZW with 9-bit starting code size. */
export function lzwEncode(pixels) {
  let codeSize = 9;
  let nextCode = 258;
  let dict = new Map();

  const bytes = [];
  let bitBuffer = 0;
  let bitCount = 0;

  const emit = (code) => {
    bitBuffer |= code << bitCount;
    bitCount += codeSize;
    while (bitCount >= 8) {
      bytes.push(bitBuffer & 0xff);
      bitBuffer >>= 8;
      bitCount -= 8;
    }
  };

  emit(CLEAR_CODE);
  let prefix = pixels[0];
  for (let i = 1; i < pixels.length; i++) {
    const k = pixels[i];
    const key = (prefix << 8) | k;
    const existing = dict.get(key);
    if (existing !== undefined) {
      prefix = existing;
      continue;
    }
    emit(prefix);
    if (nextCode === 4096) {
      // Table full: clear and start over (the standard escape hatch).
      emit(CLEAR_CODE);
      dict = new Map();
      nextCode = 258;
      codeSize = 9;
    } else {
      // Widen BEFORE assigning an entry that doesn't fit the current width.
      if (nextCode >= 1 << codeSize) codeSize++;
      dict.set(key, nextCode++);
    }
    prefix = k;
  }
  emit(prefix);
  emit(EOI_CODE);
  if (bitCount > 0) bytes.push(bitBuffer & 0xff);
  return Uint8Array.from(bytes);
}

/** GIF-flavor LZW decoder — used by the test suite to round-trip the encoder. */
export function lzwDecode(minCodeSize, data, expectedPixels) {
  const CLEAR = 1 << minCodeSize;
  const EOI = CLEAR + 1;
  let codeSize = minCodeSize + 1;
  let bitPos = 0;
  // Table slots align with code values: roots 0..255 plus reserved 256/257,
  // so the first assigned entry lands at index 258.
  let table = [];
  const resetTable = () => {
    table = [];
    for (let i = 0; i < EOI + 1; i++) table.push([i]);
  };
  resetTable();

  const out = new Uint8Array(expectedPixels);
  let outPos = 0;
  let prev = null;

  const readCode = () => {
    let code = 0;
    for (let i = 0; i < codeSize; i++) {
      const byte = data[bitPos >> 3];
      if (byte === undefined) return EOI;
      code |= ((byte >> (bitPos & 7)) & 1) << i;
      bitPos++;
    }
    return code;
  };

  for (;;) {
    const code = readCode();
    if (code === EOI) break;
    if (code === CLEAR) {
      resetTable();
      codeSize = minCodeSize + 1;
      prev = null;
      continue;
    }
    let entry;
    if (code < table.length) {
      entry = table[code];
    } else if (code === table.length && prev) {
      entry = [...prev, prev[0]]; // KwKwK: the entry being defined right now
    } else {
      throw new Error(`lzwDecode: bad code ${code} (table ${table.length})`);
    }
    for (const px of entry) {
      if (outPos >= expectedPixels) throw new Error("lzwDecode: output overflow");
      out[outPos++] = px;
    }
    if (prev && table.length < 4096) {
      table.push([...prev, entry[0]]);
      if (table.length === 1 << codeSize && codeSize < 12) codeSize++;
    }
    prev = entry;
  }
  if (outPos !== expectedPixels) throw new Error(`lzwDecode: expected ${expectedPixels} pixels, got ${outPos}`);
  return out;
}
