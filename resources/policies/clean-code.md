---
applies_to: [coder, reviewer]
---
# Clean code (apply aggressively)

Write code that anyone on the team can read and change safely. Apply these on every
change; the reviewer will block on egregious violations (needless complexity,
duplication, functions doing many things, unreadable names, opacity).

## General
- Keep it simple; reduce complexity as much as possible.
- Follow the existing conventions of the code you're in.
- Leave it cleaner than you found it - but no unrelated refactoring.
- Fix the root cause, not the symptom.

## Functions
- Small, and do ONE thing.
- Descriptive names; few arguments.
- No side effects; no flag/boolean arguments (split into separate methods instead).

## Names
- Descriptive, unambiguous, pronounceable, searchable.
- Replace magic numbers with named constants.
- No type/prefix encodings in names.

## Design
- Use dependency injection.
- Prefer polymorphism to if/else or switch on a type.
- Law of Demeter: a unit knows only its direct dependencies.
- Keep configurable data at a high level; don't over-configure.

## Understandability
- Be consistent: do similar things the same way.
- Use explanatory variables; encapsulate boundary conditions in one place.
- Prefer small value objects to bare primitives.
- Avoid negative conditionals and hidden logical dependencies.

## Objects and data
- Hide internal structure; keep objects small with few instance variables.
- Don't build half-object/half-data hybrids.
- A base class knows nothing about its derivatives.

## Comments
- Prefer to explain yourself in code; don't add redundant or obvious comments.
- Use comments for intent, clarification, or warning of consequences.
- Never comment out code - delete it.

## Structure
- Related code stays vertically close; declare variables near their use.
- Keep lines short; don't break indentation; use whitespace to group related things.

## Tests
- Readable, fast, independent, repeatable; one concept asserted per test.

## Smells to avoid
- Rigidity, fragility, needless complexity, needless repetition, opacity.
