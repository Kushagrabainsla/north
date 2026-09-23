---
name: using-a-north-tool
description: "Use when a migrated flow step must perform one explicitly named North tool operation with supplied arguments."
domains: [general, engineering]
execution:
  agent: general
  tools: [$input.tool]
  approval: on_mutation
  inputs:
    type: object
    properties:
      tool:
        type: string
      arguments:
        type: object
    required: [tool, arguments]
    additionalProperties: false
  outputs:
    type: object
    properties: {}
    additionalProperties: true
  success_criteria:
    - The named tool ran with the supplied arguments or returned a concrete failure.
---
# Use one North tool

This compatibility procedure preserves older flows while keeping the runtime hierarchy skill-based.

1. Read the `tool` and `arguments` values supplied in the flow step inputs.
2. Confirm the named tool exists and its arguments match the live schema.
3. Call that tool exactly once unless its result explicitly requires a bounded retry.
4. Respect the server-enforced mutation policy. Never substitute a different mutating tool to bypass it.
5. Return the structured tool result, any failure, and the evidence needed by the next flow step.

Do not use this procedure when a purpose-built skill exists. New flows should select a domain skill that owns the complete procedure.
