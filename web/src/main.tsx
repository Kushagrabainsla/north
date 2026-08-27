import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter } from "react-router-dom";
import { bootstrapSession } from "./api";
import { App } from "./App";
import "./styles.css";

function Root() {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    bootstrapSession().then(() => setReady(true)).catch((err) => setError(String(err)));
  }, []);
  if (error) return <div className="boot"><strong>North is unavailable</strong><p>{error}</p></div>;
  if (!ready) return <div className="boot"><span className="pulse" />Connecting to North</div>;
  return <HashRouter><App /></HashRouter>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><Root /></StrictMode>);
