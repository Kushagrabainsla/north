---
name: preparing-job-applications
description: "Use when the user wants North to find bounded job matches and prepare application drafts for human review without submitting them."
domains: [general]
execution:
  agent: general
  tools:
    - browser
    - read_file
    - write_file
  approval: always
  inputs:
    type: object
    properties:
      browser_context:
        type: string
        enum: [isolated, existing]
      context_confirmed:
        type: boolean
      connect:
        type: string
      source_url:
        type: string
      resume_path:
        type: string
      preferences:
        type: object
      max_jobs:
        type: integer
    required: [browser_context, context_confirmed, source_url, resume_path, max_jobs]
    additionalProperties: false
  outputs:
    type: object
    properties:
      processed_job_ids:
        type: array
        items:
          type: string
      drafts:
        type: array
        items:
          type: object
      skipped:
        type: array
        items:
          type: object
      questions:
        type: array
        items:
          type: string
    required: [processed_job_ids, drafts, skipped, questions]
    additionalProperties: false
  success_criteria:
    - The chosen browser context and login were verified before reading jobs.
    - No more than max_jobs were processed and persistent job IDs prevented duplicates.
    - Every draft is traceable to the resume and user preferences, and no application was submitted.
---
# Prepare job applications for review

> This procedure discovers matching jobs and prepares truthful drafts. It never submits an application.

## Procedure
1. Validate the inputs. Refuse `max_jobs` below 1 or above 10. Require the user to choose the browser context after the existing-browser privacy warning; for an existing browser, require `connect`.
2. Preflight the browser. For existing context, call `browser` with `action="preflight"` and require `verified: true`. Navigate to `source_url` and deterministically verify that the expected account is logged in. If either check fails, return a blocker instead of claiming readiness.
3. Read the resume from `resume_path` and use only facts present there or in `preferences`. Never invent experience, dates, credentials, compensation, work authorization, demographic answers, or legal attestations.
4. Load the durable processed-job index from the user's North data store. Create it if absent. Use the site's stable job ID or canonical URL as the deduplication key.
5. Inspect and collect at most `max_jobs` relevant, currently open roles. Skip duplicates, missing descriptions, obvious mismatches, and applications that require unknown factual or legal answers.
6. For each retained role, prepare a review record containing the job ID, title, company, canonical URL, match reasons, concerns, proposed field values, and unanswered questions. Filling non-final fields is allowed only within the approved step. A control such as "Easy Apply" may be used only after verifying that it opens the form; never click a final Submit application, Send application, Confirm submission, or equivalent final action.
7. Persist processed IDs only for jobs actually reviewed, with timestamp and disposition. Keep per-job failures separate so one broken page does not erase successful drafts.
8. Return the contract JSON. `questions` must contain every missing answer that blocks a truthful draft. State explicitly that submission has not occurred.

## Done when
- Browser access and login are proven, the bounded and deduplicated drafts are ready for review, unknown answers are visible, and zero applications were submitted.
