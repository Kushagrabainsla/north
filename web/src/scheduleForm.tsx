import { dateInNorthTimezone, weekdayInNorthTimezone } from "./components";
import { EntityForm, Field } from "./kit";
import { hhmm } from "./schedule";

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// The fields of a stored schedule the form reads back when editing one.
export interface ScheduleFields {
  label: string;
  hour: number;
  minute: number;
  interval_minutes?: number | null;
  weekdays: number[];
}

// The form's own idea of a schedule, before it becomes a request. Days live here
// as a set of numbers because that is what the day buttons toggle; the API is
// given "daily" when none are picked, which is what an empty selection means.
// How often, as a person chooses it - one named rule rather than a set of day
// toggles they have to translate. "Every weekday" was expressible only by
// picking five buttons and knowing that meant weekdays.
export type Repeat =
  "once" | "interval" | "daily" | "weekdays" | "weekends" | "custom";

export const REPEAT_LABELS: [Repeat, string][] = [
  ["once", "Does not repeat"],
  ["interval", "Every few minutes"],
  ["daily", "Every day"],
  ["weekdays", "Every weekday (Mon to Fri)"],
  ["weekends", "Every weekend (Sat and Sun)"],
  ["custom", "Weekly on selected days…"],
];

export interface Draft {
  label: string;
  hour: number;
  minute: number;
  intervalMinutes: number;
  repeat: Repeat;
  days: number[];
  date: string;
}

const todayISO = () => dateInNorthTimezone();

export const emptyDraft = (): Draft => ({
  label: "",
  hour: 9,
  minute: 0,
  intervalMinutes: 5,
  repeat: "once",
  days: [],
  date: todayISO(),
});

// Which named rule an existing routine is already following, so opening the
// editor shows the rule rather than making the reader infer it from checkboxes.
function repeatOf(entry: ScheduleFields): Repeat {
  if (entry.interval_minutes) return "interval";
  const weekdays = entry.weekdays;
  const set = [...weekdays].sort().join(",");
  if (!set) return "daily";
  if (set === "0,1,2,3,4") return "weekdays";
  if (set === "5,6") return "weekends";
  return "custom";
}

export const draftOf = (entry: ScheduleFields): Draft => ({
  label: entry.label,
  hour: entry.hour,
  minute: entry.minute,
  intervalMinutes: entry.interval_minutes || 5,
  repeat: repeatOf(entry),
  days: [...entry.weekdays],
  date: todayISO(),
});

// One checkbox a day: a picked day has to look picked, and a plain checkbox
// says so without a style of its own.
function DayPicker({
  days,
  onChange,
}: {
  days: number[];
  onChange: (days: number[]) => void;
}) {
  const toggle = (day: number) =>
    onChange(
      days.includes(day)
        ? days.filter((d) => d !== day)
        : [...days, day].sort(),
    );
  return (
    <>
      {DAY_LABELS.map((label, day) => (
        <label key={label}>
          <input
            type="checkbox"
            checked={days.includes(day)}
            onChange={() => toggle(day)}
          />{" "}
          {label}{" "}
        </label>
      ))}
    </>
  );
}

export function ScheduleForm({
  title,
  draft,
  setDraft,
  onSubmit,
  onCancel,
  submitLabel,
  busy,
  error,
  allowOnce,
}: {
  title: string;
  draft: Draft;
  setDraft: (d: Draft) => void;
  onSubmit: () => void;
  onCancel: () => void;
  submitLabel: string;
  busy: boolean;
  error: string;
  allowOnce: boolean;
}) {
  // An empty or half-typed time field must not silently become midnight, which
  // is what `"".split(":").map(Number)` plus `|| 0` did.
  const setTime = (value: string) => {
    const [hour, minute] = value.split(":").map(Number);
    if (!Number.isFinite(hour) || !Number.isFinite(minute)) return;
    setDraft({ ...draft, hour, minute });
  };
  // Switching to "weekly on selected days" with nothing selected leaves a rule
  // that matches no day, so it starts from today rather than from nothing.
  const setRepeat = (repeat: Repeat) =>
    setDraft({
      ...draft,
      repeat,
      days:
        repeat === "custom" && !draft.days.length
          ? [weekdayInNorthTimezone()]
          : draft.days,
    });
  const noDays = draft.repeat === "custom" && !draft.days.length;
  const options = allowOnce
    ? REPEAT_LABELS
    : REPEAT_LABELS.filter(([value]) => value !== "once");

  return (
    // The failure shows in the form, beside the fields that caused it, not at
    // the top of the page.
    <EntityForm
      title={title}
      submitLabel={submitLabel}
      busy={busy}
      error={error}
      canSubmit={!noDays}
      onSubmit={onSubmit}
      onCancel={onCancel}
    >
      <Field label="Name">
        <input
          value={draft.label}
          placeholder="Optional, e.g. Morning run"
          autoFocus
          onChange={(e) => setDraft({ ...draft, label: e.target.value })}
        />
      </Field>
      <Field label="Repeats">
        <select
          value={draft.repeat}
          onChange={(e) => setRepeat(e.target.value as Repeat)}
        >
          {options.map(([value, label]) => (
            <option value={value} key={value}>
              {label}
            </option>
          ))}
        </select>
      </Field>
      {draft.repeat === "custom" && (
        <Field
          label="On these days"
          wide
          error={noDays ? "Pick at least one day." : undefined}
        >
          <DayPicker
            days={draft.days}
            onChange={(days) => setDraft({ ...draft, days })}
          />
        </Field>
      )}
      {/* A one-off happens on a date; a wall-clock routine happens at a time;
        a fixed interval carries its own period instead. */}
      {draft.repeat === "once" && (
        <Field label="Date">
          <input
            type="date"
            value={draft.date}
            min={todayISO()}
            onChange={(e) =>
              e.target.value && setDraft({ ...draft, date: e.target.value })
            }
          />
        </Field>
      )}
      {draft.repeat === "interval" ? (
        <Field label="Every (minutes)">
          <input
            type="number"
            min={1}
            step={1}
            value={draft.intervalMinutes}
            onChange={(e) =>
              setDraft({
                ...draft,
                intervalMinutes: Math.max(1, Number(e.target.value) || 1),
              })
            }
          />
        </Field>
      ) : (
        <Field label="Time">
          <input
            type="time"
            value={hhmm(draft.hour, draft.minute)}
            onChange={(e) => setTime(e.target.value)}
          />
        </Field>
      )}
    </EntityForm>
  );
}
