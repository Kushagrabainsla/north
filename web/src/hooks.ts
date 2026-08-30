import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

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
  const resource = useResource<{ status: string }>("/health", 5000);
  const online = resource.data?.status === "ok" && !resource.error;
  const state = resource.loading && !resource.data ? "checking" : online ? "online" : "offline";
  return { ...resource, online, state };
}
