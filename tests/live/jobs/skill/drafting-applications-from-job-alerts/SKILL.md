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
  - browser
  - read_file
  - write_file
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
  - Every new job from the alert emails was either drafted or skipped with a reason, at most max_jobs drafted.
  - No application was submitted, and no link from the email was opened as written.
  - Every drafted application has one card listing every answer, with unknown answers left empty.
---

Turn new LinkedIn job-alert emails into filled-in applications the user
submits themselves. **North never submits an application.**

1. **What was already handled.** Read `~/.north/notes/jobs/seen.json` with
   read_file. It maps a job id to what happened to it. If it does not exist,
   nothing was handled yet.
2. **New alerts.** Search Gmail with
   `from:jobalerts-noreply@linkedin.com newer_than:2d` and read each message.
3. **The jobs in them.** Each job is a title, a company, a location and a
   "View job:" link. The job id is the number after `/jobs/view/`. Build the
   job's address as the link's scheme and host plus `/jobs/view/<id>/` and
   nothing after it. **Never open a link from the email as written**: it
   carries a one-time sign-in token. Skip every id already in seen.json.
4. **Which to apply to.** Read the resume at `resume_path` with read_file.
   Keep the jobs that fit `preferences` and the resume; skip the rest with a
   one-line reason. Draft at most `max_jobs`.
5. **Fill each kept job's application.** Use the browser with the given
   `browser_context`, `context_confirmed` and `connect`. Open the job's
   address, read the posting, follow its apply link to the application form,
   and fill only answers the resume or `preferences` support. **Leave every
   other field empty**: never guess salary, dates, work authorization,
   demographic or legal answers. **Never click Submit, Send, Apply or any
   final button.** Leave the filled form open.
6. **One card per job, without waiting.** Call `request_approval` with
   `wait: false`, title `<Company> - <Role>: ready for you to submit`, a
   message saying the form is filled and open in the browser and that the
   user submits it themselves, `fields` holding every form field with the
   value you entered (empty when unknown, editable), and `context` holding
   the job's address and a short summary of the posting.
7. **Remember them.** Write seen.json back with write_file, adding every job
   id you drafted or skipped, with the date and what happened.
8. Return `{"drafted": [ids], "skipped": [{"id", "reason"}]}`.
