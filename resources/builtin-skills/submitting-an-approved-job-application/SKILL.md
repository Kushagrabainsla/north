---
name: submitting-an-approved-job-application
description: "Use when the user has reviewed one specific job application and wants North to submit exactly that approved application."
domains: [general]
execution:
  agent: general
  tools:
    - browser
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
      job_id:
        type: string
      application_url:
        type: string
      approved_answers:
        type: object
    required: [browser_context, context_confirmed, job_id, application_url, approved_answers]
    additionalProperties: false
  outputs:
    type: object
    properties:
      job_id:
        type: string
      submitted:
        type: boolean
      confirmation:
        type: string
      submitted_at:
        type: string
    required: [job_id, submitted, confirmation, submitted_at]
    additionalProperties: false
  success_criteria:
    - The approval covered one named job and the exact answer set used for submission.
    - The submitted values matched the approved answers without invention or substitution.
    - A deterministic confirmation was observed and recorded, or submitted is false with a clear blocker.
---
# Submit one approved job application

> This procedure owns one consequential final action. Approval for one job never authorizes another.

## Procedure
1. Require one `job_id`, one canonical `application_url`, and the complete `approved_answers` snapshot. Reject batches, wildcards, or instructions such as "submit all".
2. Preflight the selected browser context and verify the expected account is logged in. Stop if the page or account differs from the approved target.
3. Re-open the application and compare every visible answer with `approved_answers`. Do not infer or alter factual, legal, demographic, compensation, work-authorization, or relocation answers.
4. If the site asks a new question, changes the role, or shows materially different terms, stop with `submitted: false`. A prior approval does not cover changed content.
5. Submit exactly once. Do not retry a timed-out final click unless the page is first checked for an existing confirmation or duplicate application.
6. Assert a deterministic success state such as a confirmation message or application status. Record the stable confirmation and timestamp in the user's North data store.
7. Return the contract JSON. Never report submission from a click alone.

## Done when
- One approved application was submitted exactly once and confirmation was observed, or the output truthfully records that no submission occurred.
