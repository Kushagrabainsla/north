import { useState } from "react";
import { post } from "../api";
import { BrowserProfiles } from "../browserProfiles";
import { CodingAgents, ProviderConnections, Secrets, TelegramConnection } from "../connections";
import { configureDisplayTimezone, PageHeader, Panel, YoloBadge } from "../components";
import { useResource } from "../hooks";
import { Button, Empty, Field, Loading, Notice, SectionHeader, Tabs, Note } from "../kit";

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

export const POWER_LEVELS = ["eco", "cruise", "sport"];

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
    return <Notice tone="error">Could not read the model catalog: {catalog.error}</Notice>;
  if (!providers.length)
    return (
      <Empty>
        No models are reachable. Check your provider keys under System.
      </Empty>
    );
  return (
    <div className="model-picker">
      <Field label="Provider">
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
      </Field>
      <Field label="Model">
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
      </Field>
    </div>
  );
}

export function SettingsPage() {
  const resource = useResource<SettingsData>("/orchestrator/settings");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const update = async (body: Partial<SettingsData>) => {
    // A dial is one choice at a time: a second press while the first saves is dropped.
    if (busy) return;
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
  const pickRouting = (value: "auto" | "manual") => {
    if (busy) return;
    if (value === "auto") {
      setChoosing(false);
      void update({ routing: "auto" });
    } else if (resource.data?.model) void update({ routing: "manual" });
    else setChoosing(true);
  };
  return (
    <div className="page">
      <PageHeader
        title="Settings"
        subtitle="Control how North balances capability, cost, autonomy, and readability."
        actions={<Button to="/setup">Setup guide</Button>}
      />
      {error && <Notice tone="error">{error}</Notice>}
      <div className="settings-grid">
        <Panel title="Model routing" label="Who picks">
          <Tabs
            label="Who picks the model"
            value={manual || choosing ? "manual" : "auto"}
            onChange={pickRouting}
            items={[{ value: "auto", label: "auto" }, { value: "manual", label: "manual" }]}
          />
          <Note>
            {manual
              ? "One model answers everything. North's ranking is skipped, and a model it cannot reach fails the call rather than falling back."
              : choosing
                ? "Pick the model that should answer everything."
                : "North ranks every model it can reach against what each part of a task needs, and calls them in that order."}
          </Note>
          {(manual || choosing) && (
            <ModelPicker
              value={resource.data?.model || ""}
              busy={busy}
              onPick={(spec) => {
                setChoosing(false);
                void update({ routing: "manual", model: spec });
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
          <Tabs
            label="Power"
            value={resource.data?.power || ""}
            onChange={(value) => { if (!manual) void update({ power: value }); }}
            items={POWER_LEVELS.map((value) => ({ value, label: value }))}
          />
          <Note>
            {manual
              ? "Nothing to order while one model is pinned. Switch routing to auto to use this."
              : "Choose how aggressively North selects capable models."}
          </Note>
        </Panel>
        <Panel title="Autonomy" label="Approval behavior">
          <Tabs
            label="Approval mode"
            value={autonomy || ""}
            onChange={(value) => void update({ autonomy: value })}
            items={autonomyOptions.map((option) => ({ value: option.value, label: option.value }))}
          />
          <Note>
            {currentMode?.description}
            <YoloBadge mode={autonomy} />
          </Note>
        </Panel>
        <Panel title="Time zone" label="Dates and schedules">
          {resource.loading ? (
            <Loading />
          ) : (
            <Field label="North uses">
              <select
                value={resource.data?.timezone || "UTC"}
                disabled={busy}
                onChange={(event) => void update({ timezone: event.target.value })}
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
            </Field>
          )}
          <Note>
            Current North time: {resource.data?.local_time || "–"}. New
            schedules and times without an explicit zone use this setting.
          </Note>
        </Panel>
      </div>
      <SectionHeader title="Connections" description="What North connects to. Setup walks through these same controls." />
      <div className="settings-grid">
        <ProviderConnections />
        <CodingAgents />
        <BrowserProfiles />
        <TelegramConnection />
        <Secrets />
      </div>
    </div>
  );
}
