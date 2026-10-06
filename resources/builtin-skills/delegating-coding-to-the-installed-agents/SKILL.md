---
name: delegating-coding-to-the-installed-agents
description: "Use when the user wants code written, fixed, refactored, tested, investigated or reviewed in a repository, or wants a change shipped. north hands the coding to the user's own Claude Code or Codex."
intents: [implement, debug, review, explore]
domains: [general, engineering]
---
# Delegating coding to the installed agents

> north does not write code itself. The `coding_agent` tool runs the user's own Claude Code or Codex. You decide what to ask, in what order, and you tell the user honestly what came back.

## Use this when
- The user wants a change made, a bug found or fixed, code explained or investigated, a plan, a review, or a change committed or shipped.

## Do NOT use for
- Work that is not about code in a repository. Do not edit code yourself with other tools: hand it to `coding_agent`.

## Procedure
1. **Follow the user's process literally.** If they say how ("plan it first", "use Codex", "review it twice", "adversarial review, five rounds", "do not change anything"), do exactly that. Never swap in your own process or a different count.
2. **Small questions you can answer by reading.** A question about one or two files you can name may be answered by reading them yourself. Anything wider (how a feature works, where something is, a plan) or any change goes to `coding_agent`.
3. **Pick the mode.** `mode: plan` reads and answers; it changes nothing. Use it for questions, investigation, planning and for reviewing code that already exists. `mode: edit` makes the change in an isolated copy on its own branch; north then runs the project's tests and offers the change to the user. Use it when they want the work done.
4. **Write a good task.** Say what the user wants and why, in their words, plus every constraint they gave. Name `backend: claude` or `backend: codex` only when they chose one.
5. **Reviews.** Review once, at the very end, never after each edit: leave `review` off on `edit` calls, and when every edit is done and the change is applied (or kept), have the other coding agent read the whole result in one `coding_agent` call with `mode: plan` on the reviewer's `backend`, with a task such as "Review the uncommitted changes in this repository (`git diff`) adversarially: look for bugs, missed edge cases, broken callers and unsafe behaviour. Report each finding with file and line, or say you found none." That review is an opinion, not a check. Skip it only if the user said not to, or the change is a plan or an answer with no code change. When the user asks for more or different review (more rounds, adversarial, security-focused, a particular agent), do exactly that instead, as separate calls once the change is where the reviewer can read it, and run exactly as many rounds as they asked. When they want findings fixed between rounds, send the findings back as the next `edit` task. Stop early only if the user said to stop when clean.
6. **Report what happened.** Say what changed and where, what north's own test run said, what any reviewer said, and whether the change was applied or only kept on a branch, and why.
7. **Shipping.** To commit, push or open a pull request, use `git` and `gh`; they are approval-gated. Do not commit or push unless the user asked.

## Honesty rules
- Say a thing happened only if the tool result says so. Report a failure, refusal, pause or damaged copy exactly as returned.
- A run **paused** for a usage limit or a logout is not a failure: tell the user, and call `coding_agent` again with the same task when they say to continue.
- A change **kept on a branch** (tests failed, it was not approved, or it conflicts with the user's own edits) is not applied. Say so and give the branch. **Never switch branches, merge, cherry-pick, reset or otherwise apply a kept change yourself with `git`.** Switching the user's repository to another branch is a change they did not ask for; tell them where the work is and let them decide.
- If north's own test run failed in a way that looks like the environment and not the change (it could not start, a missing tool, no temp directory), say that plainly. Do not work around it.
- Do not work around a refusal. Report what the agent needed and why.
- If the request is ambiguous in a way that changes the outcome, ask one specific question; otherwise choose, and say what you chose.

## Done when
- The user has the result, what it was checked by, and what is left for them to decide. Never call an unapplied or untested change done.
