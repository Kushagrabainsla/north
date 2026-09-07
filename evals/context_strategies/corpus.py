"""Synthetic corpora whose answers are known by construction.

The material is generated rather than borrowed so every task has exact ground
truth - no model judges another model here. It is shaped like what north
actually holds: one short fact per line about a person, the same shape as the
fact store and as a repo sweep's worth of grep hits.

Task complexity is the axis that matters. Zhang et al. (arXiv:2512.24601) find
that a model's usable context depends on how much of the input the answer needs,
not just on how long the input is, so the three task kinds here need O(1), O(n)
and O(n^2) of the corpus respectively.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field, replace

_FIRST_NAMES = (
    "Dana", "Priya", "Omar", "Lena", "Marcus", "Ines", "Yusuf", "Clara",
    "Tomas", "Aisha", "Niall", "Rosa", "Kenji", "Marta", "Idris", "Freya",
)
_LAST_NAMES = (
    "Ruiz", "Okafor", "Lindqvist", "Baptiste", "Moreau", "Kovac", "Haddad",
    "Ferreira", "Nakamura", "Andersen", "Silva", "Novak", "Weber", "Duarte",
)
_EMPLOYERS = (
    "Meridian Labs", "Foxglove Systems", "Aster Robotics", "Kestrel Analytics",
    "Northwind Freight", "Cobalt Health", "Vellum Press", "Harbourline Energy",
)
_ROLES = (
    "data engineer", "staff nurse", "logistics planner", "copy editor",
    "field technician", "research assistant", "account manager", "site surveyor",
)
_CITIES = (
    "Lisbon", "Tallinn", "Kyoto", "Bergen", "Cork", "Valencia", "Rotterdam", "Gdansk",
)

# The needle task hides one line of a different shape among the person records.
_NEEDLE_LINE = "Maintenance note: the access code for the Halden project is {code}."
_NEEDLE_QUESTION = "What is the access code for the Halden project? Answer with the code alone."

# How many record pairs share a project code. Fixed, so the answer's size is
# not a function of the corpus length.
_PLANTED_PAIRS = 4


@dataclass(frozen=True)
class Person:
    name: str
    employer: str
    role: str
    city: str
    year: int
    project: str = ""

    def as_line(self, index: int) -> str:
        return (
            f"Record {index:04d}: {self.name} works at {self.employer} "
            f"as a {self.role} in {self.city}, joined in {self.year}, "
            f"on project {self.project or f'PRJ-{index:05d}'}."
        )


@dataclass(frozen=True)
class Task:
    """One question over one corpus, with the answer already known."""

    id: str
    kind: str  # needle | aggregate | pairs | control
    complexity: str  # how much of the corpus the answer needs: O(1) | O(n) | O(n^2)
    question: str
    context: str
    answer: object  # str for needle, int for aggregate, set[frozenset[str]] for pairs
    scorer: str  # name of the grader in grading.py
    people: tuple[Person, ...] = field(default=(), repr=False)

    @property
    def context_chars(self) -> int:
        return len(self.context)

    @property
    def gold_reply(self) -> str:
        """What a perfect model would reply. Only used to self-test the harness."""
        if self.scorer == "pairs_f1":
            pairs = [sorted(pair) for pair in self.answer]
            return json.dumps({"pairs": pairs})
        return str(self.answer)


def _people(count: int, rng: random.Random) -> list[Person]:
    """Distinct people, with employers reused so shared-employer pairs exist."""
    seen: set[str] = set()
    people: list[Person] = []
    while len(people) < count:
        name = f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}"
        if name in seen:
            continue
        seen.add(name)
        people.append(
            Person(
                name=name,
                employer=rng.choice(_EMPLOYERS),
                role=rng.choice(_ROLES),
                city=rng.choice(_CITIES),
                year=rng.randint(2008, 2024),
            )
        )
    return people


def _corpus_text(people: list[Person]) -> str:
    return "\n".join(person.as_line(i) for i, person in enumerate(people))


def _people_for_chars(target_chars: int, seed: int) -> list[Person]:
    """Enough people that the rendered corpus is about `target_chars` long."""
    rng = random.Random(seed)
    people = _people(16, rng)
    while len(_corpus_text(people)) < target_chars:
        people.extend(_people(16, random.Random(seed + len(people))))
    return people


def needle_task(target_chars: int, seed: int = 0) -> Task:
    """O(1): one line answers it, the rest of the corpus is noise."""
    rng = random.Random(seed)
    people = _people_for_chars(target_chars, seed)
    code = f"{rng.randint(1000, 9999)}-{rng.choice('QXZKPV')}{rng.choice('QXZKPV')}"
    lines = [person.as_line(i) for i, person in enumerate(people)]
    lines.insert(len(lines) // 2, _NEEDLE_LINE.format(code=code))
    return Task(
        id=f"needle_{target_chars // 1000}k",
        kind="needle",
        complexity="O(1)",
        question=_NEEDLE_QUESTION,
        context="\n".join(lines),
        answer=code,
        scorer="exact",
        people=tuple(people),
    )


def aggregate_task(target_chars: int, seed: int = 0) -> Task:
    """O(n): every line has to be read, because any line may be a hit."""
    people = _people_for_chars(target_chars, seed)
    employer = _EMPLOYERS[seed % len(_EMPLOYERS)]
    count = sum(1 for person in people if person.employer == employer)
    return Task(
        id=f"aggregate_{target_chars // 1000}k",
        kind="aggregate",
        complexity="O(n)",
        question=(
            f"How many people in the records work at {employer}? "
            "Answer with the number alone."
        ),
        context=_corpus_text(people),
        answer=count,
        scorer="number",
        people=tuple(people),
    )


def pairs_task(target_chars: int, seed: int = 0, planted: int = _PLANTED_PAIRS) -> Task:
    """O(n^2): the answer is about pairs, so no single line contains it.

    The matching pairs are planted rather than found, so the answer is the same
    size whether the corpus is 8K or 1M. Otherwise the number of pairs would
    explode with corpus length (5,792 of them at 256K chars) and every strategy
    would score zero for running out of output tokens - measuring the token
    limit instead of the strategy.
    """
    rng = random.Random(seed + 977)
    people = _people_for_chars(target_chars, seed)
    chosen = rng.sample(range(len(people)), planted * 2)
    pairs: set[frozenset[str]] = set()
    for pair_index in range(planted):
        left, right = chosen[pair_index * 2], chosen[pair_index * 2 + 1]
        # Looks like every other code: a name containing "SHARED" would let a
        # single grep answer a task meant to need the whole corpus.
        code = f"PRJ-{rng.randint(10_000, 99_999)}"
        people[left] = replace(people[left], project=code)
        people[right] = replace(people[right], project=code)
        pairs.add(frozenset({people[left].name, people[right].name}))

    return Task(
        id=f"pairs_{target_chars // 1000}k",
        kind="pairs",
        complexity="O(n^2)",
        question=(
            "Two records share a project code. Find every such pair. "
            'Reply with JSON only: {"pairs": [["<name>", "<name>"], ...]}.'
        ),
        context=_corpus_text(people),
        answer=pairs,
        scorer="pairs_f1",
        people=tuple(people),
    )


def control_task(seed: int = 0) -> Task:
    """A short task the strategies must not make worse.

    Zhang et al. report a scaffold that lifts long-context scores while *halving*
    short reasoning scores (MATH 26.0 -> 5.6). A control is how that shows up
    here instead of being discovered in production.
    """
    people = _people_for_chars(2_000, seed)
    # Plant a strictly earliest joiner. Generated years tie - three people shared
    # 2008 on the first run - and a tie makes the task unanswerable rather than
    # hard: every strategy "failed" a control that had no single right answer.
    earliest_index = random.Random(seed + 31).randrange(len(people))
    people[earliest_index] = replace(
        people[earliest_index], year=min(person.year for person in people) - 1
    )
    earliest = people[earliest_index]
    return Task(
        id="control_short",
        kind="control",
        complexity="O(n)",
        question="Who joined earliest? Answer with their full name alone.",
        context=_corpus_text(people),
        answer=earliest.name,
        scorer="exact",
        people=tuple(people),
    )


def build_tasks(sizes: list[int], seed: int = 0) -> list[Task]:
    """The task matrix: three complexities at each size, plus one control."""
    tasks = [control_task(seed)]
    for size in sizes:
        tasks.append(needle_task(size, seed))
        tasks.append(aggregate_task(size, seed))
        tasks.append(pairs_task(size, seed))
    return tasks
