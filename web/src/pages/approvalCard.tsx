// The one card for anything waiting on you: a question North asks, or an action
// it wants your OK for. The Approvals page and the chat both show this card, so
// a card reads and answers the same wherever you meet it.

import { useState, type ReactNode } from "react";
import { Markdown, timeAgo } from "../components";
import { ActionBar, Button, ChoiceList, EntityForm, Field, OtherAnswer, Stack, WaitingCard } from "../kit";
import type { Approval, CardField } from "../types";

export const fieldLabel = (field: CardField) =>
  field.label || field.name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

/** Picks one short value for a field: the chosen one is pressed. */
function Choices({ options, chosen, onChoose }: { options: string[]; chosen: string; onChoose: (option: string) => void }) {
  return <ActionBar align="start">
    {options.map((option) => <Button key={option} size="sm" aria-pressed={chosen === option} onClick={() => onChoose(option)}>{option}</Button>)}
  </ActionBar>;
}

// One filled-in field. Read-only ones render as text so the card reads as work
// to check rather than a form to fill: the point is to see what North put there,
// and only the parts it offered as editable invite typing.
export function CardFieldRow({ field, value, onChange, asking = false }: {
  field: CardField;
  value: unknown;
  onChange: (v: unknown) => void;
  // A question's answer, not work to check: no "editable" mark.
  asking?: boolean;
}) {
  const text = value === null || value === undefined ? "" : String(value);
  let control;
  if (!field.editable) {
    control = field.type === "link"
      ? <a href={text} target="_blank" rel="noreferrer">{text}</a>
      : <div>{field.type === "boolean" ? (value ? "yes" : "no") : text || "—"}</div>;
  } else if (field.type === "textarea") {
    control = <textarea value={text} rows={6} onChange={(e) => onChange(e.target.value)}/>;
  } else if (field.type === "boolean") {
    control = <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)}/>;
  } else if (field.type === "select") {
    control = <select value={text} onChange={(e) => onChange(e.target.value)}>{field.options.map((o) => <option key={o} value={o}>{o}</option>)}</select>;
  } else {
    control = <input type={field.type === "number" ? "number" : "text"} value={text} placeholder={asking ? "Your answer" : undefined} onChange={(e) => onChange(e.target.value)}/>;
  }
  // Suggested answers are buttons in the field's group, not inside a <label>: a
  // label passes a click on the question to its first button, which would pick
  // the first option.
  const suggested = field.editable && field.type === "text" && field.options.length > 0;
  return <Field label={fieldLabel(field)} hint={field.editable && !asking ? "You can edit this." : undefined}>
    {suggested && <Choices options={field.options} chosen={text} onChoose={onChange}/>}
    {control}
  </Field>;
}

// Every question on a card that asks several is answered: only then are they sent, together.
export const answersComplete = (card: Approval, values: Record<string, unknown>) =>
  card.fields.every((field) => String(values[field.name] ?? "").trim() !== "");

// A rejection without a reason says only "no", which cannot be learned from -
// and for prepared work it is the highest-quality signal north ever gets. The
// reasons that come up over and over, plus free text for the one that does not.
export const REJECTION_REASONS = ["Not relevant", "Wrong details", "Already handled", "Bad timing", "Not interested"];

export function hasOwnOptions(options: string[]): boolean {
  return options.some((option) => option !== "Approve" && option !== "Reject");
}

/** A card's own options in the kit's order: the rejecting ones first, the card's
 *  first other option last, as the one primary. */
export function orderOwnOptions(options: string[], rejecting: string[]): { option: string; rejects: boolean; primary: boolean }[] {
  const rejects = options.filter((option) => rejecting.includes(option));
  const others = options.filter((option) => !rejecting.includes(option));
  const [main, ...rest] = others;
  return [
    ...rejects.map((option) => ({ option, rejects: true, primary: false })),
    ...rest.map((option) => ({ option, rejects: false, primary: false })),
    ...(main === undefined ? [] : [{ option: main, rejects: false, primary: true }]),
  ];
}

export type Decide = (decision: string, option: string, values: Record<string, unknown>, reason?: string) => void | Promise<void>;

type Values = Record<string, unknown>;
interface Part { card: Approval; values: Values; setValue: (name: string, value: unknown) => void; onDecide: Decide; aside?: ReactNode; meta: string }

/** `wide` is where there is room for prepared work's source beside it: the Approvals page. */
export function ApprovalCard({ card, onDecide, wide = false }: { card: Approval; onDecide: Decide; wide?: boolean }) {
  const fields = card.fields || [];
  const [values, setValues] = useState<Values>(() => Object.fromEntries(fields.map((f) => [f.name, f.value])));
  const setValue = (name: string, value: unknown) => setValues((current) => ({ ...current, [name]: value }));
  const part: Part = {
    card, values, setValue, onDecide,
    aside: <span>{[!card.blocking && "nothing is waiting on this", timeAgo(card.created_at)].filter(Boolean).join(" · ")}</span>,
    meta: "",
  };
  return card.type === "question" ? <QuestionCard {...part}/> : <ApproveCard {...part} wide={wide}/>;
}

function ApproveCard({ card, values, setValue, onDecide, aside, meta, wide }: Part & { wide: boolean }) {
  const fields = card.fields || [];
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const edited = fields.filter((f) => f.editable && values[f.name] !== f.value).length;
  let actions: ReactNode;
  if (rejecting) {
    // Asked only for prepared work. A guard-rail rejection is a decision about
    // one action in one task, not an example of what should be proposed, so
    // interrupting it for a reason would cost a click and teach nothing.
    actions = <EntityForm level={3} title="Why reject it?" description="This is what makes the next batch better." submitLabel="Reject"
      onSubmit={() => onDecide("rejected", "", values, reason)} onCancel={() => { setRejecting(false); setReason(""); }}>
      <Field label="Common reasons" wide><Choices options={REJECTION_REASONS} chosen={reason} onChoose={setReason}/></Field>
      <Field label="In your own words" wide><input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="or say why in your own words…"/></Field>
    </EntityForm>;
  } else if (hasOwnOptions(card.options)) {
    // A card with options of its own acts on the one you choose. Plain
    // Approve and Reject closed a Resume/Discard card and did nothing.
    actions = <ActionBar>{orderOwnOptions(card.options, card.rejecting || []).map(({ option, rejects, primary }) =>
      <Button key={option} variant={primary ? "primary" : "secondary"} onClick={() => onDecide(rejects ? "rejected" : "approved", option, values)}>{option}</Button>)}
    </ActionBar>;
  } else {
    // Reject, then Approve as the main action, last.
    actions = <ActionBar>
      <Button onClick={() => (card.source ? setRejecting(true) : onDecide("rejected", "", values))}>Reject</Button>
      <Button variant="primary" onClick={() => onDecide("approved", "", values)}>Approve</Button>
    </ActionBar>;
  }
  // Prepared work is judged against its source, so where there is room the source
  // sits beside it. For a guard-rail the source is incidental and stays folded.
  return <WaitingCard label="Needs your OK" aside={aside} title={card.title} source={card.context} sourceBeside={wide && !card.blocking}
    meta={[meta, edited > 0 && `${edited} field${edited > 1 ? "s" : ""} edited`].filter(Boolean).join(" · ")} actions={actions}>
    {/* Markdown, as it is written: shown raw, a command read "```git checkout -- draft.txt```". */}
    {card.message && <Markdown>{card.message}</Markdown>}
    {fields.length > 0 && <Stack>{fields.map((field) => <CardFieldRow key={field.name} field={field} value={values[field.name]} onChange={(v) => setValue(field.name, v)}/>)}</Stack>}
    {/* What approving will actually do. Two cards with identical buttons can submit an application and save a draft. */}
    {card.next_step && <p>Approving will <b>{card.next_step}</b>.</p>}
  </WaitingCard>;
}

function QuestionCard({ card, values, setValue, onDecide, aside, meta }: Part) {
  const fields = card.fields || [];
  const [answer, setAnswer] = useState("");
  const [index, setIndex] = useState(0);
  const shown = fields.filter((field) => !field.editable);
  const readOnly = shown.length > 0 && <Stack>{shown.map((field) => <CardFieldRow key={field.name} field={field} value={values[field.name]} onChange={() => undefined}/>)}</Stack>;
  const questions = fields.filter((field) => field.editable);

  // One question: options are shortcuts, and it can always be answered in your own words.
  if (!questions.length) {
    return <WaitingCard label="North asks" aside={aside} title={<Markdown>{card.message}</Markdown>} source={card.context} meta={meta}>
      {readOnly}
      <ChoiceList options={card.options} onPick={(option) => onDecide("answered", option, values)}/>
      <OtherAnswer collapsed={card.options.length > 0} value={answer} onChange={setAnswer} onSubmit={() => onDecide("answered", answer.trim(), values)} submitLabel="Answer"/>
    </WaitingCard>;
  }

  // Several questions: one at a time, all sent together once each has an answer.
  const at = Math.min(index, questions.length - 1);
  const field = questions[at];
  const last = at === questions.length - 1;
  const value = values[field.name];
  const text = value === null || value === undefined ? "" : String(value);
  const next = () => setIndex(at + 1);
  const withOptions = field.options.length > 0 && (field.type === "text" || field.type === "select");
  const top = <>{aside}{at > 0 && <Button variant="quiet" size="sm" onClick={() => setIndex(at - 1)}>Back</Button>}</>;
  const actions = last
    ? <ActionBar><Button variant="primary" disabled={!answersComplete(card, values)} onClick={() => onDecide("answered", "", values)}>Send answers</Button></ActionBar>
    : text.trim() && <ActionBar><Button onClick={next}>Next</Button></ActionBar>;
  return <WaitingCard label={questions.length > 1 ? `Question ${at + 1} of ${questions.length}` : "North asks"} aside={top}
    title={fieldLabel(field)} source={at === 0 ? card.context : undefined} meta={meta} actions={actions || undefined}>
    {at === 0 && readOnly}
    {withOptions && <ChoiceList options={field.options} chosen={value} onPick={(option) => { setValue(field.name, option); if (!last) next(); }}/>}
    {field.type === "text"
      ? <OtherAnswer key={field.name} collapsed={withOptions} value={field.options.includes(text) ? "" : text} onChange={(typed) => setValue(field.name, typed)} onSubmit={last ? undefined : next} submitLabel="Next"/>
      : !withOptions && <CardFieldRow field={field} asking value={value} onChange={(changed) => setValue(field.name, changed)}/>}
  </WaitingCard>;
}
