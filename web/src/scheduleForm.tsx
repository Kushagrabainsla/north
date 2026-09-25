import { dateInNorthTimezone, weekdayInNorthTimezone } from "./components";
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

// The rule as the API takes it. Only "custom" needs the day list.
export const daysField = (draft: Draft) =>
  draft.repeat === "custom"
    ? draft.days
    : draft.repeat === "once"
      ? "daily"
      : draft.repeat;

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
    <div className="day-picker">
      {DAY_LABELS.map((label, day) => (
        <button
          type="button"
          key={label}
          aria-pressed={days.includes(day)}
          className={days.includes(day) ? "day on" : "day"}
          onClick={() => toggle(day)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function ScheduleForm({
  draft,
  setDraft,
  onSubmit,
  onCancel,
  submitLabel,
  busy,
  error,
  allowOnce,
}: {
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
    <form
      className="schedule-form"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <label>
        Name
        <input
          value={draft.label}
          placeholder="Optional, e.g. Morning run"
          autoFocus
          onChange={(e) => setDraft({ ...draft, label: e.target.value })}
        />
      </label>
      <label>
        Repeats
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
      </label>
      {draft.repeat === "custom" && (
        <label>
          On these days
          <DayPicker
            days={draft.days}
            onChange={(days) => setDraft({ ...draft, days })}
          />
        </label>
      )}
      <div className="schedule-form-row">
        {/* A one-off happens on a date; a wall-clock routine happens at a time;
          a fixed interval carries its own period instead. */}
        {draft.repeat === "once" && (
          <label>
            Date
            <input
              type="date"
              value={draft.date}
              min={todayISO()}
              onChange={(e) =>
                e.target.value && setDraft({ ...draft, date: e.target.value })
              }
            />
          </label>
        )}
        {draft.repeat === "interval" ? (
          <label>
            Every (minutes)
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
          </label>
        ) : (
          <label>
            Time
            <input
              type="time"
              value={hhmm(draft.hour, draft.minute)}
              onChange={(e) => setTime(e.target.value)}
            />
          </label>
        )}
      </div>
      {/* Beside the field that caused it, not at the top of the page. */}
      {error && <p className="schedule-form-error">{error}</p>}
      {noDays && <p className="schedule-form-error">Pick at least one day.</p>}
      <div className="schedule-form-actions">
        <button
          type="submit"
          className="primary-button"
          disabled={busy || noDays}
        >
          {busy ? "Saving…" : submitLabel}
        </button>
        <button type="button" className="ghost-button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

// An in-page confirmation. window.confirm is an operating-system dialog in an
// app that styles everything else itself, and it cannot say what will happen
// afterwards.
export function ConfirmRow({
  question,
  confirmLabel,
  onConfirm,
  onCancel,
  busy,
}: {
  question: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  busy: boolean;
}) {
  return (
    <div className="schedule-confirm">
      <span>{question}</span>
      <div className="schedule-actions">
        <button
          className="ghost-button danger-link"
          onClick={onConfirm}
          disabled={busy}
        >
          {confirmLabel}
        </button>
        <button className="ghost-button" onClick={onCancel} disabled={busy}>
          Keep it
        </button>
      </div>
    </div>
  );
}
