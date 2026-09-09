// Asking a question inside the app, rather than through the browser.
//
// `window.confirm` and friends are operating-system dialogs in an app that
// styles everything else itself: they arrive in a different typeface, in a
// different place, wearing the browser's name rather than north's. They also
// say very little - the button is always "OK", never "Delete this rule", so the
// only description of what is about to happen is the sentence above it. And
// they block the main thread, which stops the polling and the event stream
// underneath them.
//
// `ConfirmRow` in the Schedule page already made this argument for one case.
// This is the same idea as something any page can call.
//
// The API mirrors the three it replaces, so a call site changes by one keyword:
//
//     if (!window.confirm(q)) return;      →   if (!await confirm(q)) return;
//     const t = window.prompt(q, initial); →   const t = await prompt(q, initial);
//     window.alert(msg);                   →   await alert(msg);
//
// Built on <dialog>, so focus trapping, Escape, inertness of the page behind and
// the top-layer stacking are the browser's job rather than ours - those are the
// details hand-rolled modals get wrong.

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

type Kind = "confirm" | "prompt" | "alert";

interface Request {
  kind: Kind;
  message: string;
  title?: string;
  /** Names the action instead of "OK", so the button says what it will do. */
  confirmLabel?: string;
  /** Marks a destructive action, so it does not look like an ordinary save. */
  danger?: boolean;
  initial?: string;
  resolve: (value: boolean | string | null) => void;
}

export interface AskOptions {
  title?: string;
  confirmLabel?: string;
  danger?: boolean;
}

interface DialogApi {
  confirm: (message: string, options?: AskOptions) => Promise<boolean>;
  prompt: (message: string, initial?: string, options?: AskOptions) => Promise<string | null>;
  alert: (message: string, options?: AskOptions) => Promise<void>;
}

const DialogContext = createContext<DialogApi | null>(null);

export function useDialog(): DialogApi {
  const api = useContext(DialogContext);
  if (!api) throw new Error("useDialog must be used inside <DialogProvider>");
  return api;
}

export function DialogProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<Request | null>(null);
  const [draft, setDraft] = useState("");
  const ref = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);

  const ask = useCallback((req: Omit<Request, "resolve">) => {
    return new Promise<boolean | string | null>(resolve => {
      setDraft(req.initial ?? "");
      setRequest({ ...req, resolve });
    });
  }, []);

  const api = useRef<DialogApi>({
    confirm: (message, options) => ask({ kind: "confirm", message, ...options }) as Promise<boolean>,
    prompt: (message, initial = "", options) =>
      ask({ kind: "prompt", message, initial, ...options }) as Promise<string | null>,
    alert: (message, options) => ask({ kind: "alert", message, ...options }).then(() => undefined),
  }).current;

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (request && !element.open) {
      element.showModal();
      // The input for a prompt, so it can be typed into straight away; otherwise
      // the confirm button, so Enter answers and Escape cancels.
      (request.kind === "prompt" ? input.current : element.querySelector<HTMLButtonElement>(".dialog-confirm"))?.focus();
    }
    if (!request && element.open) element.close();
  }, [request]);

  // Escape closes a <dialog> without going through our buttons, so the promise
  // has to be settled here too - a caller left awaiting forever is a hung page.
  const close = (value: boolean | string | null) => {
    request?.resolve(value);
    setRequest(null);
  };
  const cancel = () => close(request?.kind === "prompt" ? null : false);

  return <DialogContext.Provider value={api}>
    {children}
    <dialog className="dialog" ref={ref} onCancel={event => { event.preventDefault(); cancel(); }}
      onClick={event => { if (event.target === ref.current) cancel(); }}>
      {request && <form method="dialog" className="dialog-body"
        onSubmit={event => { event.preventDefault(); close(request.kind === "prompt" ? draft : true); }}>
        <h2>{request.title || (request.kind === "alert" ? "Heads up" : "Are you sure?")}</h2>
        <p>{request.message}</p>
        {request.kind === "prompt" && <input ref={input} value={draft} autoComplete="off"
          onChange={event => setDraft(event.target.value)}/>}
        <div className="dialog-actions">
          {request.kind !== "alert" && <button type="button" className="ghost-button" onClick={cancel}>Cancel</button>}
          <button type="submit"
            className={`dialog-confirm ${request.danger ? "danger-button" : "primary-button"}`}>
            {request.confirmLabel || (request.kind === "alert" ? "Got it" : "Confirm")}
          </button>
        </div>
      </form>}
    </dialog>
  </DialogContext.Provider>;
}
