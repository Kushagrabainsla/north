---
name: observability-and-instrumentation
description: "Use when adding a feature or a hard-to-debug path that will need logs, metrics, or traces to operate and diagnose in production."
---
# Observability and instrumentation

> **Instrument the exact question you will need to ask in production.**

## Use this when
- You are adding behaviour whose health/failures you will need to see at runtime.

## Do NOT use for
- Throwaway scripts, or pure logic with no runtime to observe.

## Procedure
1. State the operational question the signal must answer ("is X failing? how slow is Y?").
2. Add the smallest log/metric/trace that answers it - structured fields, consistent level.
3. Include a correlation id so one request can be followed across components.
4. Never log secrets or PII.
5. Make it actionable (feeds an alert or a dashboard line), not noise.

## Done when
- The signal answers its question, is structured, and leaks nothing sensitive.
