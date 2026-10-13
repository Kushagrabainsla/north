import { useState } from "react";
import { post } from "../api";
import { BrowserProfiles } from "../browserProfiles";
import { CodingAgents, ProviderConnections } from "../connections";
import { configureDisplayTimezone, Empty, ErrorNotice, Loading, PageHeader, Panel, YoloBadge } from "../components";
import { useResource } from "../hooks";
import { Link } from "react-router-dom";
import type { Approval } from "../types";

export interface ChainModel {
  model: string;
  score: number;
  price: number | null;
  providers: string[];
  available: boolean;
  skipped_because: string;
}
/** One part of a task, and the models north would try for it, in order. */
export interface PartChain {
  part: string;
  requires: string[];
  order_by: string;
  min_context: number;
  eligible: number;
  models: ChainModel[];
}

export interface ModeOption {
  value: string;
  description: string;
}
export interface SettingsData {
  power: string;
  autonomy: string;
  // The approval modes, served by the API so every surface lists the same ones.
  autonomy_options: ModeOption[];
  routing: string;
  model: string;
  timezone: string;
  timezone_options: string[];
  local_time: string;
}
export interface ProviderModels {
  provider: string;
  models: string[];
}

// Which model answers, when the user is choosing it rather than north. Provider
// first, then that provider's models: a flat list of several hundred ids is not
// a choice anyone can make, and the provider is the half people know.
export function ModelPicker({
  value,
  onPick,
  busy,
}: {
  value: string;
  onPick: (spec: string) => void;
  busy: boolean;
}) {
  const catalog = useResource<ProviderModels[]>(
    "/orchestrator/inference/catalog",
  );
  const providers = catalog.data || [];
  // A stored pin is "provider:model_id", but only the provider half is a fixed
  // vocabulary - a model id may itself contain a colon, so it is split once.
  const [chosenProvider, chosenModel] = (() => {
    const at = value.indexOf(":");
    return at > 0 ? [value.slice(0, at), value.slice(at + 1)] : ["", value];
  })();
  const provider = chosenProvider || providers[0]?.provider || "";
  const models =
    providers.find((row) => row.provider === provider)?.models || [];

  if (catalog.loading) return <Loading />;
  if (catalog.error)
    return (
      <ErrorNotice
        message={`Could not read the model catalog: ${catalog.error}`}
      />
    );
  if (!providers.length)
    return (
      <Empty>
        No models are reachable. Check your provider keys under System.
      </Empty>
    );
  return (
    <div className="model-picker">
      <label>
        Provider
        <select
          value={provider}
          disabled={busy}
          onChange={(event) =>
            onPick(
              `${event.target.value}:${(providers.find((r) => r.provider === event.target.value)?.models || [])[0] || ""}`,
            )
          }
        >
          {providers.map((row) => (
            <option value={row.provider} key={row.provider}>
              {row.provider}
            </option>
          ))}
        </select>
      </label>
      <label>
        Model
        <select
          value={chosenModel}
          disabled={busy || !models.length}
          onChange={(event) => onPick(`${provider}:${event.target.value}`)}
        >
          {(models.includes(chosenModel) || !chosenModel
            ? models
            : [chosenModel, ...models]
          ).map((id) => (
            <option value={id} key={id}>
              {id}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

export function SettingsPage() {
  const resource = useResource<SettingsData>("/orchestrator/settings");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const update = async (body: Partial<SettingsData>) => {
    setBusy(true);
    setError("");
    try {
      const saved = await post<SettingsData>("/orchestrator/settings", body);
      configureDisplayTimezone(saved.timezone);
      await resource.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const routing = resource.data?.routing || "auto";
  const autonomy = resource.data?.autonomy;
  const autonomyOptions = resource.data?.autonomy_options || [];
  const currentMode = autonomyOptions.find(
    (option) => option.value === autonomy,
  );
  const manual = routing === "manual";
  // Manual routing needs a model, and the server refuses the switch without one.
  // So choosing "manual" with nothing stored opens the picker and waits: the
  // switch is made by picking a model, which is the decision anyway.
  const [choosing, setChoosing] = useState(false);
  const pickManual = () =>
    resource.data?.model ? update({ routing: "manual" }) : setChoosing(true);
  return (
    <div className="page">
      <PageHeader
        eyebrow="Configuration"
        title="Settings"
        subtitle="Control how North balances capability, cost, autonomy, and readability."
        actions={<Link className="ghost-button" to="/setup">Setup guide</Link>}
      />
      {error && <ErrorNotice message={error} />}
      <div className="settings-grid">
        <Panel title="Model routing" label="Who picks">
          <div className="segmented">
            <button
              className={!manual && !choosing ? "active" : ""}
              disabled={busy}
              onClick={() => {
                setChoosing(false);
                update({ routing: "auto" });
              }}
            >
              auto
            </button>
            <button
              className={manual || choosing ? "active" : ""}
              disabled={busy}
              onClick={pickManual}
            >
              manual
            </button>
          </div>
          <p className="muted">
            {manual
              ? "One model answers everything. North's ranking is skipped, and a model it cannot reach fails the call rather than falling back."
              : choosing
                ? "Pick the model that should answer everything."
                : "North ranks every model it can reach against what each part of a task needs, and calls them in that order."}
          </p>
          {(manual || choosing) && (
            <ModelPicker
              value={resource.data?.model || ""}
              busy={busy}
              onPick={(spec) => {
                setChoosing(false);
                update({ routing: "manual", model: spec });
              }}
            />
          )}
        </Panel>
        {/* Ordering a chain of one is meaningless, so under manual the dial is
          shown switched off rather than left looking live. */}
        <Panel
          title="Power"
          label={manual ? "not in use" : "Model strategy"}
          className={manual ? "panel-inert" : ""}
        >
          <div className="segmented">
            {["eco", "cruise", "sport"].map((value) => (
              <button
                className={resource.data?.power === value ? "active" : ""}
                disabled={busy || manual}
                onClick={() => update({ power: value })}
                key={value}
              >
                {value}
              </button>
            ))}
          </div>
          <p className="muted">
            {manual
              ? "Nothing to order while one model is pinned. Switch routing to auto to use this."
              : "Choose how aggressively North selects capable models."}
          </p>
        </Panel>
        <Panel title="Autonomy" label="Approval behavior">
          <div className="segmented">
            {autonomyOptions.map((option) => (
              <button
                className={autonomy === option.value ? "active" : ""}
                disabled={busy}
                onClick={() => update({ autonomy: option.value })}
                title={option.description}
                key={option.value}
              >
                {option.value}
              </button>
            ))}
          </div>
          <p className="muted">
            {currentMode?.description}
            <YoloBadge mode={autonomy} />
          </p>
        </Panel>
        <Panel title="Time zone" label="Dates and schedules">
          {resource.loading ? (
            <Loading />
          ) : (
            <label className="timezone-picker">
              North uses
              <select
                value={resource.data?.timezone || "UTC"}
                disabled={busy}
                onChange={(event) => update({ timezone: event.target.value })}
              >
                {(
                  resource.data?.timezone_options || [
                    resource.data?.timezone || "UTC",
                  ]
                ).map((timezone) => (
                  <option value={timezone} key={timezone}>
                    {timezone.replaceAll("_", " ")}
                  </option>
                ))}
              </select>
            </label>
          )}
          <p className="muted">
            Current North time: {resource.data?.local_time || "–"}. New
            schedules and times without an explicit zone use this setting.
          </p>
        </Panel>
      </div>
      <h2 className="section-title">Connections</h2>
      <p className="muted memory-note">What North connects to. Setup walks through these same controls.</p>
      <div className="settings-grid">
        <ProviderConnections />
        <CodingAgents />
        <BrowserProfiles />
      </div>
    </div>
  );
}

