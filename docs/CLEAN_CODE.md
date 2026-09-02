# Clean Code Checklist

> The bar every change in north is held to. `docs/CODING_STYLE.md` is the
> project-specific rulebook and wins wherever the two overlap; this file is the
> general checklist behind it, kept whole so it can be read on its own.

**Code is clean if it can be understood easily - by everyone on the team.** Clean
code can be read and enhanced by a developer other than its original author. With
understandability comes readability, changeability, extensibility and
maintainability.

---

## General rules

1. Follow standard conventions.
2. Keep it simple. Simpler is always better. Reduce complexity as much as possible.
3. Boy scout rule. Leave the campground cleaner than you found it.
4. Always find the root cause. Never patch a symptom you have not explained.

## Design rules

1. Keep configurable data at high levels.
2. Prefer polymorphism to if/else or switch/case.
3. Separate multi-threading code.
4. Prevent over-configurability.
5. Use dependency injection.
6. Follow the Law of Demeter. A class should know only its direct dependencies.

## Understandability tips

1. Be consistent. If you do something a certain way, do all similar things the same way.
2. Use explanatory variables.
3. Encapsulate boundary conditions. They are hard to keep track of; put their processing in one place.
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
6. Don't use flag arguments. Split the method into several independent methods
   that can be called from the client without the flag.

## Comments

1. Always try to explain yourself in code.
2. Don't be redundant.
3. Don't add obvious noise.
4. Don't use closing brace comments.
5. Don't comment out code. Just remove it.
6. Use comments to explain intent.
7. Use comments to clarify code.
8. Use comments to warn of consequences.

## Source code structure

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
4. Should be small.
5. Do one thing.
6. Small number of instance variables.
7. A base class should know nothing about its derivatives.
8. Better to have many functions than to pass some code into a function to select a behaviour.
9. Prefer non-static methods to static methods.

## Tests

1. One assert per test.
2. Readable.
3. Fast.
4. Independent.
5. Repeatable.

## Code smells

1. **Rigidity.** The software is difficult to change. A small change causes a cascade of subsequent changes.
2. **Fragility.** The software breaks in many places due to a single change.
3. **Immobility.** You cannot reuse parts of the code in other projects because of involved risks and high effort.
4. **Needless complexity.**
5. **Needless repetition.**
6. **Opacity.** The code is hard to understand.
