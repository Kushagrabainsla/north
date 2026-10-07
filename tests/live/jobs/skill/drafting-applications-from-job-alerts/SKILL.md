---
name: drafting-applications-from-job-alerts
description: Use when turning new LinkedIn job-alert emails into filled-in, unsubmitted job applications that the user reviews and submits themselves.
source: learned
status: candidate
domains:
- general
intents: []
execution:
  agent: general
  tools:
  - mcp__gmail__search_gmail_messages
  - mcp__gmail__get_gmail_message_content
  - offered_before
  - browser
  - read_file
  inputs:
    type: object
    properties:
      resume_path:
        type: string
      preferences:
        type: string
      max_jobs:
        type: integer
      browser_context:
        type: string
        enum: [isolated, existing]
      context_confirmed:
        type: boolean
      connect:
        type: string
    required: [resume_path, preferences, max_jobs, browser_context, context_confirmed]
    additionalProperties: false
  outputs:
    type: object
    properties:
      drafted:
        type: array
        items:
          type: string
      skipped:
        type: array
        items:
          type: object
    required: [drafted, skipped]
  approval: on_mutation
  success_criteria:
  - Every job not offered before was either drafted or skipped with a reason, at most max_jobs drafted.
  - No application was submitted, nothing on a form was clicked, and no link from the email was opened as written.
  - Every drafted application has one card listing every answer, with unknown answers left empty.
---

Turn new LinkedIn job-alert emails into filled-in applications the user
submits themselves. **North never submits an application.**

1. **New alerts.** Search Gmail with
   `from:jobalerts-noreply@linkedin.com newer_than:2d` and read each message.
2. **The jobs in them.** Each job is a title, a company, a location and a
   "View job:" link. The job id is the number after `/jobs/view/`. Build the
   job's address as the link's scheme and host plus `/jobs/view/<id>/` and
   nothing after it. **Never open a link from the email as written**: it
   carries a one-time sign-in token.
3. **Skip what was offered before.** For each job, call `offered_before`
   with `item_keys` = [the job's address, "<Company> | <Role>"]. Skip every
   job it reports as offered, before doing any work on it.
4. **Which to apply to.** Read the resume at `resume_path` with read_file.
   Keep the jobs that fit `preferences` and the resume; skip the rest with a
   one-line reason. Draft at most `max_jobs`.
5. **Fill each kept job's application.** Use the browser with the given
   `browser_context`, `context_confirmed` and `connect`. Open the job's
   address and read the posting. Find its apply link's address with the
   browser's `inspect` or `read` (never by running JavaScript) and **open
   that address with `goto`; do not click it.**
   Fill every answer the resume or `preferences` support, including yes/no
   questions they settle (work authorization, sponsorship), with `fill` or
   `select`. **Leave every other field empty**: never guess salary, dates,
   demographic or legal answers. **Never click anything on the form, never
   press Enter, never submit.** Leave the filled form open.
6. **One card per job, without waiting.** Call `request_approval` with
   `wait: false`, the same `item_keys` as in step 3, title
   `<Company> - <Role>: ready for you to submit`, a message saying the form
   is filled and open in the browser and that the user submits it
   themselves, `fields` holding every form field with the value you entered
   (empty when unknown, editable), and `context` holding the job's address
   and a short summary of the posting.
7. Return `{"drafted": [ids], "skipped": [{"id", "reason"}]}`.
