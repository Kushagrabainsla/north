import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter } from "react-router-dom";
import { api, bootstrapSession } from "./api";
import { App } from "./App";
import { configureDisplayTimezone } from "./components";
import { isTypeScale, readPersistentValue, UI_PREFERENCE_KEYS } from "./hooks";
import "./styles.css";

// Restore document-wide presentation before the first paint. The matching
// Settings control uses the persistent-state hook for subsequent changes.
document.documentElement.dataset.typeScale = readPersistentValue(
  UI_PREFERENCE_KEYS.typeScale, "comfortable", isTypeScale,
);

function Root() {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    bootstrapSession()
      .then(() => api<{ timezone: string }>("/orchestrator/settings"))
      .then(settings => { configureDisplayTimezone(settings.timezone); setReady(true); })
      .catch((err) => setError(String(err)));
  }, []);
  if (error) return <div className="boot"><strong>North is unavailable</strong><p>{error}</p></div>;
  if (!ready) return <div className="boot"><span className="pulse" />Connecting to North</div>;
  return <HashRouter><App /></HashRouter>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><Root /></StrictMode>);
