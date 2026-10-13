/**
 * The physical key a printable character comes from on a US keyboard.
 *
 * A key event's windowsVirtualKeyCode names the key, not the character, and
 * only letters and digits share a number with their ASCII code. Punctuation
 * does not: "." is 46, which is VK_DELETE, and "#" is 35, which is VK_END, so
 * sending charCodeAt as the key code makes Chrome edit instead of insert.
 */

// [code, vk] for each key, then the character it types unshifted and shifted.
const KEYS = [
  ["Backquote", 192, "`", "~"],
  ["Digit1", 49, "1", "!"],
  ["Digit2", 50, "2", "@"],
  ["Digit3", 51, "3", "#"],
  ["Digit4", 52, "4", "$"],
  ["Digit5", 53, "5", "%"],
  ["Digit6", 54, "6", "^"],
  ["Digit7", 55, "7", "&"],
  ["Digit8", 56, "8", "*"],
  ["Digit9", 57, "9", "("],
  ["Digit0", 48, "0", ")"],
  ["Minus", 189, "-", "_"],
  ["Equal", 187, "=", "+"],
  ["BracketLeft", 219, "[", "{"],
  ["BracketRight", 221, "]", "}"],
  ["Backslash", 220, "\\", "|"],
  ["Semicolon", 186, ";", ":"],
  ["Quote", 222, "'", '"'],
  ["Comma", 188, ",", "<"],
  ["Period", 190, ".", ">"],
  ["Slash", 191, "/", "?"],
  ["Space", 32, " ", null],
];

const BY_CHAR = new Map();
for (const [code, vk, plain, shifted] of KEYS) {
  BY_CHAR.set(plain, { code, vk, shift: false });
  if (shifted) BY_CHAR.set(shifted, { code, vk, shift: true });
}
for (let c = 65; c <= 90; c++) {
  const upper = String.fromCharCode(c);
  BY_CHAR.set(upper.toLowerCase(), { code: "Key" + upper, vk: c, shift: false });
  BY_CHAR.set(upper, { code: "Key" + upper, vk: c, shift: true });
}

/** The unshifted character of a key named by its code ("Period" → "."), or undefined. */
export function charOfCode(code) {
  for (const [ch, k] of BY_CHAR) if (k.code === code && !k.shift) return ch;
}

/**
 * { code, vk, shift } for a character a US keyboard types, or, for anything
 * else (accents, other scripts, emoji), no key at all: vk 0 and an empty code,
 * which Chrome inserts from the event's text without treating it as a command.
 */
export function usKey(ch) {
  return BY_CHAR.get(ch) ?? { code: "", vk: 0, shift: false };
}
