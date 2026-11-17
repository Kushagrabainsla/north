import { useState } from "react";
import { post } from "../api";
import { BrowserProfiles } from "../browserProfiles";
import { CodingAgents, ProviderConnections, Secrets, TelegramConnection } from "../connections";
import { configureDisplayTimezone, PageHeader, Panel, YoloBadge } from "../components";
import { useResource } from "../hooks";
import { Button, Empty, Field, ListRow, Loading, Notice, SectionHeader, Tabs, Note } from "../kit";

export interface ModeOption {
  value: string;
  description: string;
}
/** One row of the Models panel: a model as "provider:model_id", or "" for Auto ranked by power. */
export interface ModelRow {
  model: string;
  power: string;
}
/** A kind of work, and its own row - null when it is "Same as default". */
export interface KindRow {
  kind: string;
  label: string;
  description: string;
  choice: ModelRow | null;
}
export interface ModelsData {
  default: ModelRow;
  kinds: KindRow[];
}
export interface SettingsData {
  power: string;
  autonomy: string;
  // The approval modes, served by the API so every surface lists the same ones.
  autonomy_options: ModeOption[];
  models: ModelsData;
  timezone: string;
  timezone_options: string[];
  local_time: string;
}
export type SettingsUpdate = Partial<Omit<SettingsData, "models">> & {
  models?: Record<string, ModelRow | null>;
};

export type RowMode = "same" | "auto" | "model";

export const rowMode = (choice: ModelRow | null): RowMode =>
  !choice ? "same" : choice.model ? "model" : "auto";

/** What a row runs on, in a few words: the model's own name, or Auto and its power. */
export const describeChoice = (choice: ModelRow): string =>
  // Split once: a model id may itself contain a colon.
  choice.model ? choice.model.slice(choice.model.indexOf(":") + 1) : `Auto, ${choice.power}`;
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
          {/* With nothing chosen, a placeholder: else a provider with one model offers no change to pick. */}
          {!chosenModel && <option value="" disabled>Choose a model</option>}
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

/** One row of the Models panel. "Choose a model" saves nothing until a model is picked. */
function ModelRowEditor({
  label,
  description,
  choice,
  fallback,
  busy,
  onSave,
}: {
  label: string;
  description: string;
  choice: ModelRow | null;
  // The Default row, which a kind "Same as default" uses. Absent on the Default row itself.
  fallback?: ModelRow;
  busy: boolean;
  onSave: (choice: ModelRow | null) => void;
}) {
  const [choosing, setChoosing] = useState(false);
  const mode = choosing ? "model" : rowMode(choice);
  const power = choice?.power || "cruise";
  const pick = (next: RowMode) => {
    if (busy || next === mode) return;
    setChoosing(next === "model");
    if (next === "same") onSave(null);
    if (next === "auto") onSave({ model: "", power });
  };
  return (
    <ListRow title={label} description={description}>
      <div className="model-row">
        <Field label="Uses">
          <Tabs
            label={`${label} uses`}
            value={mode}
            disabled={busy}
            onChange={pick}
            items={[
              // Short, read after "Uses": they share one line on a phone.
              ...(fallback ? [{ value: "same" as RowMode, label: "default" }] : []),
              { value: "auto" as RowMode, label: "auto" },
              { value: "model" as RowMode, label: "model" },
            ]}
          />
        </Field>
        {mode === "auto" && (
          <Field label="Power">
            <Tabs
              label={`${label} power`}
              value={power}
              disabled={busy}
              onChange={(value) => onSave({ model: "", power: value })}
              items={POWER_LEVELS.map((value) => ({ value, label: value }))}
            />
          </Field>
        )}
      </div>
      {mode === "same" && fallback && <Note>Same as the Default row: {describeChoice(fallback)}.</Note>}
      {mode === "model" && (
        <ModelPicker
          value={choice?.model || ""}
          busy={busy}
          onPick={(spec) => {
            setChoosing(false);
            onSave({ model: spec, power });
          }}
        />
      )}
    </ListRow>
  );
}

export function SettingsPage() {
  const resource = useResource<SettingsData>("/orchestrator/settings");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const update = async (body: SettingsUpdate) => {
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
  const autonomy = resource.data?.autonomy;
  const autonomyOptions = resource.data?.autonomy_options || [];
  const currentMode = autonomyOptions.find(
    (option) => option.value === autonomy,
  );
  const models = resource.data?.models;
  return (
    <div className="page">
      <PageHeader
        title="Settings"
        subtitle="Which models north uses, how it asks, and what it connects to."
        actions={<Button to="/setup">Setup guide</Button>}
      />
      {error && <Notice tone="error">{error}</Notice>}
      <div className="settings-grid">
        {/* The one place models and power are shown and set (#80). */}
        <Panel title="Models" className="models-panel">
          <Note>
            Auto: north ranks the models it can reach for each part of the work. Eco tries the cheapest
            first, cruise the best fit, sport the most capable. A model you choose is the only one
            called: if it fails, the work stops.
          </Note>
          {resource.loading && !models ? (
            <Loading />
          ) : models ? (
            <div>
              <ModelRowEditor
                label="Default"
                description="What every kind of work below uses, unless you change its row."
                choice={models.default}
                busy={busy}
                onSave={(choice) => choice && void update({ models: { default: choice } })}
              />
              {models.kinds.map((row) => (
                <ModelRowEditor
                  key={row.kind}
                  label={row.label}
                  description={row.description}
                  choice={row.choice}
                  fallback={models.default}
                  busy={busy}
                  onSave={(choice) => void update({ models: { [row.kind]: choice } })}
                />
              ))}
            </div>
          ) : null}
        </Panel>
        <Panel title="Autonomy">
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
        <Panel title="Time zone">
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
