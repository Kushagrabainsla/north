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
1. Validate the inputs. Refuse `max_jobs` below 1 or above 10. Require explicit browser-context confirmation, and require `connect` for an existing browser.
2. Preflight the existing CDP endpoint with `browser action="preflight"` and require `verified: true`, or check isolated-browser status. Navigate to `source_url` and assert that the expected account is logged in.
3. Read the resume and preferences. Never invent experience, dates, credentials, compensation, work authorization, demographic answers, legal attestations, or relocation intent.
4. Load the persistent job index from the user's North data store. Discover at most `max_jobs` jobs, identify each by stable site ID or canonical URL, and skip already reviewed entries.
5. For each viable job, inspect the complete posting and application. Fill only answers supported by the resume or preferences. If an answer is unknown, do not guess; record the blocker and move to the next job.
6. Immediately before any final submission, call `request_approval` with a title naming the company and role, a message stating that approval submits this one application, structured `fields` containing every proposed answer, and `context` containing the job URL and relevant posting text. Make factual fields editable when the user may correct them; keep the job ID and URL read-only.
7. If rejected or unanswered, do not submit and record the job as rejected or blocked. If approved, use the returned `response` values, reconcile them against the visible form, and submit exactly once. A decision for one job never authorizes another.
8. After a final click, assert a confirmation message or submitted status. Do not retry until checking for an existing submission. Persist the job ID, decision, confirmation, and timestamp.
9. Return the contract JSON. A click without confirmation is blocked, not submitted.

## Done when
- Every bounded job has a durable disposition, each submission has item-specific reviewed approval and confirmation, and rejected or unanswered jobs remain unsubmitted.
