---
name: processing-a-job-application-queue
description: "Use when the user wants North to open a bounded queue of matching jobs, fill truthful application drafts, request approval per job, and submit only approved applications."
domains: [general]
execution:
  agent: general
  tools:
    - browser
    - read_file
    - write_file
  approval: before_step
  inputs:
    type: object
    properties:
      profile_id:
        type: string
      source_url:
        type: string
      resume_path:
        type: string
      preferences:
        type: object
      max_jobs:
        type: integer
    required: [source_url, resume_path, max_jobs]
    additionalProperties: false
  outputs:
    type: object
    properties:
      reviewed_job_ids:
        type: array
        items:
          type: string
      submitted_job_ids:
        type: array
        items:
          type: string
      rejected_job_ids:
        type: array
        items:
          type: string
      blocked:
        type: array
        items:
          type: object
    required: [reviewed_job_ids, submitted_job_ids, rejected_job_ids, blocked]
    additionalProperties: false
  success_criteria:
    - Browser attachment and login were proven before processing the queue.
    - Work was bounded and deduplicated using stable persisted job IDs.
    - Every final submission had its own approval card with the exact answers and source job visible.
    - Every reported submission has a deterministic confirmation and no rejected application was submitted.
---
# Process a job application queue

> This procedure handles a bounded queue end to end. Initial step approval authorizes preparation only; every application still requires its own final approval.

## Procedure
1. Validate the inputs. Refuse `max_jobs` below 1 or above 10. Call `browser:list_profiles` and choose an enabled profile by task context and saved purpose, or use `profile_id` when given; ask through the central approval policy if unclear. Pass `profile_id` per browser call, never bind it to the flow.
2. Preflight with `browser action="preflight"` and require `profile_verified: true`. Navigate to `source_url` and assert that the expected account is logged in. Attach the resume to file inputs with `action="upload"` and `filename=resume_path`.
3. Read the resume and preferences. Never invent experience, dates, credentials, compensation, work authorization, demographic answers, legal attestations, or relocation intent.
4. Load the persistent job index from the user's North data store. Discover at most `max_jobs` jobs, identify each by stable site ID or canonical URL, and skip already reviewed entries.
5. For each viable job, inspect the complete posting and application. Fill only answers supported by the resume or preferences. If an answer is unknown, do not guess; record the blocker and move to the next job.
6. Immediately before filling the form, call `request_approval` once with a title naming the company and role, a message stating that approval fills these answers and submits this one application (with the job URL), and `calls` listing every fill and the final submit click exactly as you will make them. Raise no other card for this application.
7. If rejected or unanswered, do not fill or submit, and record the job as rejected or blocked. If approved, make exactly those calls, in order, once each. A decision for one job never authorizes another.
8. After a final click, assert a confirmation message or submitted status. Do not retry until checking for an existing submission. Persist the job ID, decision, confirmation, and timestamp.
9. Return the contract JSON. A click without confirmation is blocked, not submitted.

## Done when
- Every bounded job has a durable disposition, each submission has item-specific reviewed approval and confirmation, and rejected or unanswered jobs remain unsubmitted.
