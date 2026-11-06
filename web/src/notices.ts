// What a notice says, read three ways: the file it saved, its body to read in full, and a short
// plain preview for the dashboard. A notice can carry a whole briefing ("daily-research-briefing is
// ready" is the briefing itself); shown whole on the dashboard, it stretched the Attention panel to
// hundreds of lines and the panels beside it into empty boxes.

const SAVED_AT = /\n*_Saved at (.+?)_\s*$/;

/** The file a notice's run saved, from its "_Saved at …_" line or a JSON answer naming its artifacts. */
export function savedFile(message: string): string {
  const saved = SAVED_AT.exec(message);
  if (saved) return saved[1].trim();
  return artifactsIn(message)[0] ?? "";
}

/** The notice to read in full: its text without the saved-file line, which is shown as a link instead. */
export function noticeBody(message: string): string {
  if (artifactsIn(message).length) return "";
  return message.replace(SAVED_AT, "").trim();
}

/** A short plain-text preview: the first words of the body, with Markdown's marks taken out. */
export function noticePreview(message: string, length = 220): string {
  const artifacts = artifactsIn(message);
  if (artifacts.length) return `Saved ${fileName(artifacts[0])}${artifacts.length > 1 ? ` and ${artifacts.length - 1} more` : ""}.`;
  const plain = noticeBody(message)
    .replace(/^\[cut short\]$/gm, "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/(\*\*|__|`)/g, "")
    .replace(/(^|\s)[*_]([^*_\n]+)[*_](?=\s|$|[.,;:!?])/g, "$1$2")
    .replace(/\s+/g, " ")
    .trim();
  return plain.length > length ? `${plain.slice(0, length).replace(/\s+\S*$/, "")}…` : plain;
}

/** The last part of a path: what a person calls the file. */
export const fileName = (path: string) => path.split(/[\\/]/).filter(Boolean).pop() ?? path;

function artifactsIn(message: string): string[] {
  const text = message.trim();
  if (!text.startsWith("{")) return [];
  try {
    const parsed = JSON.parse(text) as { artifacts?: unknown };
    return Array.isArray(parsed.artifacts) ? parsed.artifacts.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}
