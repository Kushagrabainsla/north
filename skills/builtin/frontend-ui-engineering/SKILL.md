---
name: frontend-ui-engineering
description: "Use when building or changing user-facing UI components (web, React, or native) - layout, state, and accessibility."
---
# Frontend UI engineering

> **Handle every UI state: loading, empty, error, success, disabled - and the keyboard.**

## Use this when
- The change renders or modifies user-facing UI.

## Do NOT use for
- Backend / non-UI code.

## Procedure
1. Enumerate the component state matrix: loading, empty, error, success, disabled, focus/keyboard.
2. Build accessible markup: labels, roles, keyboard navigation, screen-reader text.
3. Keep component state minimal and derive the rest.
4. Match the design system / tokens; avoid layout shift.
5. Test the states (at least empty, error, and success).

## Done when
- Every state renders correctly, the component is keyboard- and screen-reader-usable, and it matches the design.
