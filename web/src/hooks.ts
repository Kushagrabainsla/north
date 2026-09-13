import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

export const UI_PREFERENCE_KEYS = {
  sidebarCollapsed: "north-sidebar-collapsed",
  chatListWidth: "north-chat-list-width",
  memoryTab: "north-memory-tab",
  memoryDocument: "north-memory-document",
  typeScale: "north-type-scale",
} as const;

export const TYPE_SCALES = ["compact", "comfortable", "large"] as const;
export type TypeScale = typeof TYPE_SCALES[number];
export const isTypeScale = (value: unknown): value is TypeScale =>
  typeof value === "string" && TYPE_SCALES.some(scale => scale === value);

/** Decode a browser preference without letting stale or malformed storage break the UI. */
export function parsePersistentValue<T>(raw: string | null, fallback: T, isValid?: (value: unknown) => boolean): T {
  if (raw === null) return fallback;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    // Older North preferences were stored as plain strings. Keep them readable
    // while new values use JSON so booleans, numbers, and null retain their type.
    value = raw;
  }
  return (!isValid || isValid(value)) ? value as T : fallback;
}

/** Read one optional browser preference, including environments where storage is unavailable. */
export function readPersistentValue<T>(key: string, fallback: T, isValid?: (value: unknown) => boolean): T {
  try {
    return parsePersistentValue(window.localStorage.getItem(key), fallback, isValid);
  } catch {
    return fallback;
  }
}

/** Persist durable presentation choices in this browser. */
export function usePersistentState<T>(key: string, fallback: T, isValid?: (value: unknown) => boolean) {
  const [value, setValue] = useState<T>(() => readPersistentValue(key, fallback, isValid));
  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Preferences are optional when storage is disabled or full.
    }
  }, [key, value]);
  return [value, setValue] as const;
}

export function useResource<T>(path: string | null, refreshMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(Boolean(path));
  const reload = useCallback(async () => {
    if (!path) return;
    try {
      setError("");
      setData(await api<T>(path));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    void reload();
    if (!refreshMs) return;
    const timer = window.setInterval(reload, refreshMs);
    return () => window.clearInterval(timer);
  }, [reload, refreshMs]);
  return { data, error, loading, reload, setData };
}

export function useHealth() {
  const resource = useResource<{ status: string; checks?: Record<string, { status: string; detail?: string }> }>("/health", 5000);
  const online = resource.data?.status === "ok" && !resource.error;
  // "starting" is North warming up (provider catalogues load after the API is
  // serving), not a fault - it must not read the same as "degraded".
  const reported = resource.error ? undefined : resource.data?.status;
  const state = resource.loading && !resource.data ? "checking"
    : online ? "online"
    : reported === "starting" ? "starting"
    : reported === "degraded" ? "degraded"
    : "offline";
  return { ...resource, online, state };
}
