"""Print the profile's section headings and sizes (no content), and how many recalled facts it repeats.

Run: .venv/bin/python -m experiments.memory_recall.profile_shape
"""

from __future__ import annotations

from experiments.memory_recall.common import (
    DATA_DIR,
    PROFILE_CHAR_CAP,
    profile_sections,
    profile_text,
    read_jsonl,
    tokens,
)


def main() -> None:
    text = profile_text()
    print(f"profile chars={len(text)} cap={PROFILE_CHAR_CAP} cut={max(0, len(text) - PROFILE_CHAR_CAP)}")
    for item in profile_sections():
        print(f"{item.id:45s} tokens={tokens(item.text):5d} truncated={item.meta['truncated']}")
    cases = read_jsonl(DATA_DIR / "cases.jsonl")
    lowered = text.lower()
    repeated = total = repeated_tokens = 0
    for case in cases:
        for fact in case["facts"]:
            total += 1
            core = fact["text"].split(" (")[0].strip().rstrip(".").lower()
            if core and core in lowered:
                repeated += 1
                repeated_tokens += tokens(fact["text"])
    print(f"recalled facts={total} already in the profile={repeated} ({repeated / max(total, 1):.1%})")
    print(f"tokens of repeated facts per task={repeated_tokens / max(len(cases), 1):.0f}")


if __name__ == "__main__":
    main()
