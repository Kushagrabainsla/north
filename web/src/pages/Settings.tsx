import { useState } from "react";
import { post } from "../api";
import { BrowserProfiles } from "../browserProfiles";
import { CodingAgents, ProviderConnections, Secrets, TelegramConnection } from "../connections";
import { configureDisplayTimezone, PageHeader, Panel, YoloBadge } from "../components";
import { useResource } from "../hooks";
import { Button, Field, ListRow, Loading, Notice, SectionHeader, Tabs, Note } from "../kit";

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
export interface ProviderModels {
  provider: string;
  models: string[];
}

export const POWERS = ["eco", "cruise", "sport"];

/** A row's choice as one list value: "same", "auto:eco", or "model:provider:model_id". */
export const choiceValue = (choice: ModelRow | null): string =>
  !choice ? "same" : choice.model ? `model:${choice.model}` : `auto:${choice.power}`;

/** The choice a list value stands for. A chosen model keeps the row's power for a later Auto. */
export const valueChoice = (value: string, current: ModelRow | null): ModelRow | null => {
  if (value === "same") return null;
  if (value.startsWith("auto:")) return { model: "", power: value.slice(5) };
  return { model: value.slice(6), power: current?.power || "cruise" };
};

/** What a row runs on, in a few words: the model's own name, or Auto and its power. */
export const choiceText = (choice: ModelRow): string =>
  // Split once: a model id may itself contain a colon.
  choice.model ? choice.model.slice(choice.model.indexOf(":") + 1) : `Auto · ${choice.power}`;

/** One list for a row (#80): Same as default, Auto with each power, then each provider's models. */
export function ModelSelect({ label, choice, sameAs, catalog, busy, onChange }: {
  label: string;
  choice: ModelRow | null;
  // The Default row's choice, offered as "Same as default"; absent on the Default row itself.
  sameAs?: ModelRow;
  catalog: ProviderModels[];
  busy: boolean;
  onChange: (choice: ModelRow | null) => void;
}) {
  const value = choiceValue(choice);
  const listed = catalog.some((row) => row.models.some((id) => value === `model:${row.provider}:${id}`));
  return (
    <Field label={label} hideLabel>
      <select value={value} disabled={busy} onChange={(event) => onChange(valueChoice(event.target.value, choice))}>
        {sameAs && <option value="same">Same as default ({choiceText(sameAs)})</option>}
        <optgroup label="North chooses">
          {POWERS.map((power) => (
            <option key={power} value={`auto:${power}`}>Auto · {power}</option>
          ))}
        </optgroup>
        {/* A saved model the catalog no longer lists stays visible, rather than the list showing another. */}
        {choice?.model && !listed && (
          <optgroup label="Not reachable now">
            <option value={value}>{choice.model}</option>
          </optgroup>
        )}
        {catalog.map((row) => (
          <optgroup key={row.provider} label={row.provider}>
            {row.models.map((id) => (
              <option key={id} value={`model:${row.provider}:${id}`}>{id}</option>
            ))}
          </optgroup>
        ))}
      </select>
    </Field>
  );
}

/** The one place models and power are shown and set (#80): every row, each with one list. */
export function ModelsPanel({ models, catalog, busy, save }: {
  models: ModelsData;
  catalog: ProviderModels[];
  busy: boolean;
  /** Change rows by name: "default" or a kind; null makes a kind follow the default again. */
  save: (rows: Record<string, ModelRow | null>) => void;
}) {
  return (
    <div>
      <Note>
        Auto lets north pick, cheapest first (eco) to most capable (sport). A model you choose is the
        only one called: if it fails, the work stops.
      </Note>
      <ListRow title="Default" description="Used by every row set to Same as default."
        actions={<ModelSelect label="Default" choice={models.default} catalog={catalog} busy={busy}
          onChange={(choice) => choice && save({ default: choice })} />} />
      {models.kinds.map((row) => (
        <ListRow key={row.kind} title={row.label} description={row.description}
          actions={<ModelSelect label={row.label} choice={row.choice} sameAs={models.default} catalog={catalog}
            busy={busy} onChange={(choice) => save({ [row.kind]: choice })} />} />
      ))}
    </div>
  );
}

export function SettingsPage() {
  const resource = useResource<SettingsData>("/orchestrator/settings");
  const catalog = useResource<ProviderModels[]>("/orchestrator/inference/catalog");
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
        <Panel title="Models" className="models-panel">
          {catalog.error && <Notice tone="error">Could not read the model catalog: {catalog.error}</Notice>}
          {models ? (
            <ModelsPanel models={models} catalog={catalog.data || []} busy={busy}
              save={(rows) => void update({ models: rows })} />
          ) : resource.loading ? <Loading /> : null}
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
