// Small, pure schedule vocabulary shared by every page that says when
// something runs. One way of writing a time, so the same fact never reads as
// two different ones on adjacent panels.

export const hhmm = (hour: number, minute: number) =>
  `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;

// `now` is a parameter so a test can pin the clock.
export function whenFromNow(
  epoch: number,
  now: number = Date.now() / 1000,
): string {
  const seconds = epoch - now;
  if (seconds < 0) return "now";
  if (seconds < 90) return "in under a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `in ${minutes} min`;
  const hours = seconds / 3600;
  if (hours < 24) return `in ${Math.round(hours)} h`;
  const days = Math.round(hours / 24);
  return days === 1 ? "tomorrow" : `in ${days} days`;
}

// "5m ago", "3h ago", "2d ago" - how long since something happened.
export function agoText(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, (now - then) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
