---
name: clean-code-checklist
description: "Use when writing or reviewing any code and you need the bar it is held to - naming, function size, comments, structure, objects vs data, and the six code smells. The standing checklist behind every north change, not a refactoring procedure."
---
# Clean Code Checklist

> **Code is clean if it can be understood easily - by everyone on the team.**
> Clean code can be read and enhanced by a developer other than its original
> author. With understandability comes readability, changeability,
> extensibility and maintainability.

## Use this when
- Writing new code, before you commit to a shape
- Reviewing a diff and you need concrete criteria rather than taste
- Something feels wrong but you can't name it - the code-smell list names it

## Do NOT use for
- Deciding *whether* to refactor working code - see `code-simplification`
- Executing a refactor safely - see `safe-refactoring`
- Project-specific rules (async, ledger writes, FastAPI) - see `docs/CODING_STYLE.md`, which wins on any overlap

## General
1. Follow standard conventions.
2. Keep it simple. Simpler is always better. Reduce complexity as much as possible.
3. Boy scout rule. Leave the campground cleaner than you found it.
4. Always find the root cause. Never patch a symptom you have not explained.

## Design
1. Keep configurable data at high levels.
2. Prefer polymorphism to if/else or switch/case.
3. Separate multi-threading code.
4. Prevent over-configurability.
5. Use dependency injection.
6. Follow the Law of Demeter. A class should know only its direct dependencies.

## Understandability
1. Be consistent. If you do something a certain way, do all similar things the same way.
2. Use explanatory variables.
3. Encapsulate boundary conditions. Put their processing in one place.
4. Prefer dedicated value objects to primitive types.
5. Avoid logical dependency. Don't write methods that work correctly only because of something else in the same class.
6. Avoid negative conditionals.

## Names
1. Choose descriptive and unambiguous names.
2. Make meaningful distinctions.
3. Use pronounceable names.
4. Use searchable names.
5. Replace magic numbers with named constants.
6. Avoid encodings. Don't append prefixes or type information.

## Functions
1. Small.
2. Do one thing.
3. Use descriptive names.
4. Prefer fewer arguments.
5. Have no side effects.
6. Don't use flag arguments. Split the method into independent methods the caller picks between.

## Comments
1. Always try to explain yourself in code.
2. Don't be redundant.
3. Don't add obvious noise.
4. Don't use closing brace comments.
5. Don't comment out code. Just remove it.
6. Use comments to explain intent, clarify code, or warn of consequences.

## Source structure
1. Separate concepts vertically.
2. Related code should appear vertically dense.
3. Declare variables close to their usage.
4. Dependent functions should be close.
5. Similar functions should be close.
6. Place functions in the downward direction.
7. Keep lines short.
8. Don't use horizontal alignment.
9. Use white space to associate related things and disassociate weakly related ones.
10. Don't break indentation.

## Objects and data structures
1. Hide internal structure.
2. Prefer data structures.
3. Avoid hybrids (half object, half data).
4. Should be small, do one thing, and hold few instance variables.
5. A base class should know nothing about its derivatives.
6. Better to have many functions than to pass some code into a function to select a behaviour.
7. Prefer non-static methods to static methods.

## Tests
1. One assert per test.
2. Readable. 3. Fast. 4. Independent. 5. Repeatable.

## Code smells - name the problem
| Smell | What you'll notice |
|---|---|
| **Rigidity** | A small change causes a cascade of subsequent changes |
| **Fragility** | One change breaks the software in many unrelated places |
| **Immobility** | You cannot reuse a part elsewhere without dragging half the system with it |
| **Needless complexity** | Machinery for a requirement that does not exist |
| **Needless repetition** | The same knowledge expressed in more than one place |
| **Opacity** | The code is hard to understand |

## Applying it
Do not sweep a file against all sixty rules. Take the smell you actually hit,
find the two or three rules that explain it, and fix the root cause. A change
that satisfies the checklist but makes the diff harder to review has failed.
