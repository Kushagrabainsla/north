// Draws a view north made: A2UI v0.9 messages on north's kit catalog (views/catalog.py).
//
// Each part in the catalog is drawn by the kit part of the same role, so a view looks
// like every other page and can show nothing the kit cannot. The server checked the view
// before it got here; this still draws an unknown part as an error rather than guessing,
// and stops at a loop or a tree too deep to be a page.

import type { ReactNode } from "react";
import { Markdown, Panel } from "./components";
import {
  ActionBar, Badge, Button, Empty, EntityTable, KeyValues, ListRow, Note, Notice, SectionHeader, Stack, Stat, StatGrid,
  Status, Text, type Column, type NoticeTone,
} from "./kit";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface ViewComponent { id: string; component: string; [property: string]: unknown }
export interface ViewMessage {
  version: string;
  createSurface?: { surfaceId: string; catalogId: string };
  updateComponents?: { surfaceId: string; components: ViewComponent[] };
  updateDataModel?: { surfaceId: string; path?: string; value?: Json };
}

const MAX_DEPTH = 12;

/** The parts and the data a surface's messages describe. */
export function readMessages(messages: ViewMessage[]): { components: Map<string, ViewComponent>; data: Json } {
  const components = new Map<string, ViewComponent>();
  let data: Json = {};
  for (const message of messages) {
    for (const component of message.updateComponents?.components || []) components.set(component.id, component);
    const update = message.updateDataModel;
    if (update && (!update.path || update.path === "/")) data = update.value ?? {};
  }
  return { components, data };
}

/** `path` read from the data: from the top when it starts with "/", else from `scope`, the item a template draws. */
export function resolvePath(data: Json, path: string, scope = ""): Json | undefined {
  const full = path.startsWith("/") ? path : `${scope}/${path}`;
  let node: Json | undefined = data;
  for (const raw of full.split("/").filter(Boolean)) {
    const key = raw.replaceAll("~1", "/").replaceAll("~0", "~");
    if (Array.isArray(node)) node = node[Number(key)];
    else if (node && typeof node === "object") node = (node as Record<string, Json>)[key];
    else return undefined;
  }
  return node;
}

const absolute = (path: string, scope: string) => (path.startsWith("/") ? path : `${scope}/${path}`);

/** A property's value: as written, or read from the data when it is `{"path": ...}`. */
function value(data: Json, property: unknown, scope: string): Json | undefined {
  if (property && typeof property === "object" && !Array.isArray(property) && "path" in property) {
    return resolvePath(data, String((property as { path: unknown }).path), scope);
  }
  return property as Json | undefined;
}

function text(data: Json, property: unknown, scope: string): string {
  const resolved = value(data, property, scope);
  if (resolved === undefined || resolved === null) return "";
  return typeof resolved === "object" ? JSON.stringify(resolved) : String(resolved);
}

export interface ViewSurfaceProps {
  messages: ViewMessage[];
  /** Runs a button's action. Absent for a draft: its buttons do nothing until it is added. */
  onAction?: (name: string) => void;
  busyAction?: string;
}

export function ViewSurface({ messages, onAction, busyAction }: ViewSurfaceProps) {
  const { components, data } = readMessages(messages);

  const draw = (id: string, scope: string, trail: string[]): ReactNode => {
    const part = components.get(id);
    if (!part) return <Notice key={id} tone="error">This view names a part, {id}, that it does not have.</Notice>;
    if (trail.includes(id) || trail.length >= MAX_DEPTH) return <Notice key={id} tone="error">This view is nested too deep to draw.</Notice>;
    const inner = [...trail, id];
    const children = (): ReactNode[] => {
      const list = part.children;
      if (Array.isArray(list)) return list.map(child => <Frag key={String(child)}>{draw(String(child), scope, inner)}</Frag>);
      if (list && typeof list === "object") {
        const { path, componentId } = list as { path: string; componentId: string };
        const items = resolvePath(data, path, scope);
        const base = absolute(path, scope);
        return Array.isArray(items) ? items.map((_, index) => <Frag key={index}>{draw(componentId, `${base}/${index}`, inner)}</Frag>) : [];
      }
      return [];
    };
    const say = (property: unknown) => text(data, property, scope);

    switch (part.component) {
      case "Column": return <Stack>{children()}</Stack>;
      case "Row": return <ActionBar align="start">{children()}</ActionBar>;
      case "Panel": return <Panel title={String(part.title)} label={part.label ? String(part.label) : undefined}><Stack>{children()}</Stack></Panel>;
      case "Heading": return <SectionHeader level={3} title={say(part.text)} description={part.description ? say(part.description) : undefined}/>;
      case "Text": return <Text>{say(part.text)}</Text>;
      case "Note": return <Note>{say(part.text)}</Note>;
      case "Markdown": return <Markdown>{say(part.text)}</Markdown>;
      case "StatGrid": return <StatGrid>{children()}</StatGrid>;
      case "Stat": return <Stat label={String(part.label)} value={say(part.value)} detail={part.detail ? say(part.detail) : undefined}/>;
      case "Table": {
        const rows = value(data, part.rows, scope);
        const columns = (part.columns as { key: string; label: string }[]).map((column): Column => ({ key: column.key, label: column.label }));
        const list = Array.isArray(rows) ? rows : [];
        return <EntityTable label={id} columns={columns} rows={list.map((row, index) => ({ row, index }))} rowKey={item => String(item.index)}
          cell={(item, key) => text(item.row, { path: `/${key}` }, "")} empty={part.empty ? String(part.empty) : "Nothing to list."}/>;
      }
      case "ListItem": return <ListRow title={say(part.title)} description={part.description ? say(part.description) : undefined}
        meta={((part.meta as unknown[]) || []).map(say).filter(Boolean)} status={part.status ? <Status value={say(part.status)}/> : undefined}/>;
      case "Status": return <Status value={say(part.value)}/>;
      case "Badge": return <Badge>{say(part.text)}</Badge>;
      case "Notice": return <Notice tone={part.tone as NoticeTone}>{say(part.text)}</Notice>;
      case "Empty": return <Empty>{say(part.text)}</Empty>;
      case "KeyValues": return <KeyValues items={(part.items as { label: string; value: unknown }[]).map(item => [item.label, say(item.value)])}/>;
      case "Button": {
        const name = (part.action as { event: { name: string } }).event.name;
        return <Button variant={(part.variant as "primary" | "secondary" | "danger") || "secondary"} disabled={!onAction}
          busy={busyAction === name} busyLabel="Starting…" title={onAction ? undefined : "Its buttons work once the view is added."}
          onClick={() => onAction?.(name)}>{String(part.label)}</Button>;
      }
      case "Link": return <Button variant="quiet" to={String(part.to)}>{String(part.label)}</Button>;
      default: return <Notice tone="error">This view uses a part north's kit does not have: {part.component}.</Notice>;
    }
  };

  if (!components.has("root")) return <Notice tone="error">This view has no root part, so there is nothing to draw.</Notice>;
  return <div className="kit-view">{draw("root", "", [])}</div>;
}

function Frag({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
