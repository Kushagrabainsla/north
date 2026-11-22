import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "./api";
import type { Artifact, Conversation } from "./types";

/** One thing you can jump to: a page, a session, a flow, a file or a setting. */
interface Target { kind: string; label: string; hint?: string; go: () => void }

// Pages and the settings people look for by name. Each goes to its one home.
const PAGES: [string, string, string?][] = [
  ["Home", "/"], ["Inbox", "/inbox"], ["Inbox · For your info", "/inbox?tab=info"], ["Inbox · Done", "/inbox?tab=done"],
  ["Sessions", "/sessions"], ["Tasks", "/tasks"], ["Flows", "/flows"], ["Files", "/artifacts"],
  ["Memory", "/memory"], ["Skills", "/skills"], ["Tools", "/tools"], ["Views", "/views"], ["Devices", "/lights"],
  ["System · health, usage and cost", "/system"], ["Settings", "/settings"],
  ["Settings · Models", "/settings", "models"], ["Settings · Autonomy", "/settings", "autonomy"],
  ["Settings · Time zone", "/settings", "time zone"], ["Settings · Providers", "/settings", "api key"],
  ["Settings · Browser profiles", "/settings", "chrome"], ["Settings · Telegram", "/settings"],
  ["Memory · Safe actions", "/memory?tab=rules"], ["Memory · Learned decisions", "/memory?tab=decisions"],
  ["Memory · North stars", "/memory?tab=north_stars.md"], ["Memory · Judgement rules", "/memory?tab=judgement_rules.md"],
  ["Memory · Soul", "/memory?tab=soul.md"], ["Memory · Facts", "/memory?tab=facts"], ["Memory · Episodes", "/memory?tab=episodes"],
];

// Two-key shortcuts, as in Gmail and Linear: g then a letter.
const GO_KEYS: Record<string, string> = { h: "/", i: "/inbox", s: "/sessions", t: "/tasks", f: "/flows", a: "/artifacts", m: "/memory", y: "/system", c: "/settings" };

const typing = (target: EventTarget | null) =>
  target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));

/** Rank by where the words match: a match at the start of a word beats one inside it. */
export function score(label: string, query: string): number {
  const text = label.toLowerCase();
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  let total = 0;
  for (const word of words) {
    const at = text.indexOf(word);
    if (at < 0) return -1;
    total += at === 0 || /\W/.test(text[at - 1]) ? 2 : 1;
  }
  return total;
}

/** ⌘K or / opens a box that finds any page, session, flow, file or setting by name. */
export function CommandPalette() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const [found, setFound] = useState<Target[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const goPrefix = useRef(0);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setOpen(value => !value); return; }
      if (typing(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "/") { event.preventDefault(); setOpen(true); return; }
      if (event.key === "g") { goPrefix.current = Date.now(); return; }
      if (Date.now() - goPrefix.current < 1200 && GO_KEYS[event.key]) { goPrefix.current = 0; navigate(GO_KEYS[event.key]); }
    };
    const onOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("north:palette", onOpen);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("north:palette", onOpen); };
  }, [navigate]);

  useEffect(() => {
    if (!open) return;
    setQuery(""); setIndex(0);
    setTimeout(() => input.current?.focus(), 0);
    let live = true;
    const close = () => setOpen(false);
    Promise.allSettled([
      api<Conversation[]>("/web/api/conversations"),
      api<{ name: string; description?: string }[]>("/web/api/flow-definitions"),
      api<Artifact[]>("/web/api/artifacts"),
      api<{ name: string; description?: string }[]>("/web/api/skills"),
    ]).then(([sessions, flows, files, skills]) => {
      if (!live) return;
      const value = <T,>(result: PromiseSettledResult<T[]>) => result.status === "fulfilled" ? result.value : [];
      setFound([
        ...value(sessions).map(s => ({ kind: "Session", label: s.title || "Untitled session", go: () => { navigate(`/sessions/${s.id}`); close(); } })),
        ...value(flows).map(f => ({ kind: "Flow", label: f.name, hint: f.description, go: () => { navigate(`/flows?open=${encodeURIComponent(f.name)}`); close(); } })),
        ...value(files).map(a => ({ kind: "File", label: a.name, go: () => { window.open(`/web/api/artifacts/${encodeURIComponent(a.id)}/file`, "_blank", "noreferrer"); close(); } })),
        ...value(skills).map(s => ({ kind: "Skill", label: s.name, hint: s.description, go: () => { navigate("/skills"); close(); } })),
      ]);
    });
    return () => { live = false; };
  }, [open, navigate]);

  const results = useMemo(() => {
    const pages: Target[] = PAGES.map(([label, to, hint]) => ({ kind: "Go to", label, hint, go: () => { navigate(to); setOpen(false); } }));
    const all = [...pages, ...found];
    if (!query.trim()) return pages.slice(0, 8);
    return all
      .map(target => ({ target, rank: Math.max(score(target.label, query), target.hint ? score(target.hint, query) - 1 : -1) }))
      .filter(item => item.rank >= 0)
      .sort((a, b) => b.rank - a.rank)
      .slice(0, 12)
      .map(item => item.target);
  }, [query, found, navigate]);

  if (!open) return null;
  const choose = (target?: Target) => target?.go();
  return <div className="palette-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setOpen(false); }}>
    <div className="palette" role="dialog" aria-label="Search and jump">
      <input ref={input} value={query} placeholder="Find a page, session, flow, file or setting…" aria-label="Search"
        onChange={event => { setQuery(event.target.value); setIndex(0); }}
        onKeyDown={event => {
          if (event.key === "Escape") setOpen(false);
          if (event.key === "ArrowDown") { event.preventDefault(); setIndex(i => Math.min(i + 1, results.length - 1)); }
          if (event.key === "ArrowUp") { event.preventDefault(); setIndex(i => Math.max(i - 1, 0)); }
          if (event.key === "Enter") { event.preventDefault(); choose(results[index]); }
        }}/>
      <ul role="listbox">
        {results.map((target, i) => <li key={`${target.kind}-${target.label}-${i}`} role="option" aria-selected={i === index}
          className={i === index ? "active" : ""} onMouseEnter={() => setIndex(i)} onMouseDown={event => { event.preventDefault(); choose(target); }}>
          <span className="palette-kind">{target.kind}</span><span className="palette-label">{target.label}</span>
        </li>)}
        {!results.length && <li className="palette-empty">Nothing matches "{query}".</li>}
      </ul>
      <footer><span>↑↓ choose · Enter open · Esc close</span><span>g i Inbox · g h Home · g f Flows</span></footer>
    </div>
  </div>;
}
