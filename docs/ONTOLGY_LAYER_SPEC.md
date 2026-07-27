# North Ontology Layer — Technical Specification

**Version:** 1.0  
**Status:** Design (not implemented)  
**Author:** North System Design  
**Date:** 2026-07-26

---

## 1. Overview

### 1.1 Purpose
Add a **universal ontology layer** to North — a typed knowledge graph of the user's world state that enables structured reasoning across all life domains (health, finance, career, learning, relationships, etc.).

### 1.2 Core Concept
North currently has a **semantic layer** (embedding-based recall, LLM-mediated classification). The ontology layer adds a **symbolic layer** — typed entities, formal relationships, constraints, and deterministic reasoning — that operates *alongside* the semantic layer, not replacing it.

```
┌─────────────────────────────────────────────────────────────────┐
│                      NORTH ORCHESTRATOR                          │
├─────────────────────────────────────────────────────────────────┤
│  SEMANTIC LAYER (existing)           ONTOLOGY LAYER (new)       │
│  ┌─────────────────────────┐       ┌─────────────────────┐      │
│  │ Vector similarity       │       │ Typed entities      │      │
│  │ LLM classification      │       │ Formal relationships│      │
│  │ Fuzzy skill matching    │       │ Constraint logic    │      │
│  │ Free-text fact recall   │       │ Deterministic rules │      │
│  └───────────┬─────────────┘       └──────────┬──────────┘      │
│              │                                │                  │
│              └──────────────┬─────────────────┘                  │
│                             ▼                                    │
│                    ┌─────────────────────┐                       │
│                    │   CONTEXT ASSEMBLY  │                       │
│                    │  (merges both)      │                       │
│                    └─────────────────────┘                       │
└─────────────────────────────────────────────────────────────────┘
```

### 1.3 Design Principles
- **Universal, not domain-specific** — same 8 entity types, 10 relationship types cover all domains
- **Deterministic where it matters** — constraints, budgets, prerequisites are computed, not guessed
- **Human-in-the-loop for conflicts** — autonomy flag controls resolution behavior
- **Full provenance** — every fact traces to source (device, prompt, tool, inference)
- **Privacy-first** — all local, no boundary yet, export controls later
- **Federated-ready** — identity hooks for future peer-to-peer sync
- **Standards-aware** — live access to FHIR, schema.org, FIBO, O*NET with local mirror

---

## 2. Architecture

### 2.1 Package Structure
```
north/
├── ontology/
│   ├── __init__.py           # Public exports
│   ├── entities.py           # Entity definitions (Pydantic models)
│   ├── relationships.py      # Relationship definitions + validation
│   ├── store.py              # OntologyStore (SQLite + JSON + graph indexes)
│   ├── parser.py             # OntologyParser (LLM extraction)
│   ├── ingestion.py          # OntologyIngestion (orchestration)
│   ├── reasoner.py           # Reasoner (constraint checking, projection, simulation)
│   ├── scheduler.py          # ConstraintScheduler (OR-Tools CP-SAT)
│   ├── external.py           # ExternalOntologyClient (FHIR, schema.org, FIBO, etc.)
│   ├── models.py             # Shared enums, types, SourceType, PrivacyLevel
│   └── query.py              # GraphQuery, DecisionContext
├── skills/builtin/
│   ├── ontology-ingest/      # Skill: when/how to ingest structured data
│   ├── ontology-query/       # Skill: complex graph queries from agents
│   ├── ontology-plan/        # Skill: constraint-aware planning
│   └── ontology-simulate/    # Skill: what-if simulation
├── tools/universal/
│   ├── ontology_ingest.py    # Tool: ingest_ontology(content, source_type, source_ref)
│   ├── ontology_query.py     # Tool: query_ontology(cypher-like or structured)
│   └── ontology_simulate.py  # Tool: simulate(activities, constraints, goals)
├── cron/
│   ├── ontology_device_sync.py      # Daily: Whoop, Apple Health, Oura, etc.
│   ├── ontology_external_refresh.py # Weekly: FHIR, schema.org, FIBO updates
│   └── ontology_dedup.py            # Nightly: semantic dedup, confidence decay
└── memory/
    └── injection.py          # Extended: ContextInjector → OntologyIngestion
```

### 2.2 Data Flow

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   SOURCES    │────▶│    PARSER    │────▶│  VALIDATION  │────▶│    STORE     │
│              │     │ (LLM + schema)│    │ (Pydantic +   │     │ (SQLite +    │
│ • User prompt│     │               │     │  relationship │     │  JSON +      │
│ • Tool output│     │ OntologyParser│     │  rules +      │     │  graph idx)  │
│ • Device sync│     │               │     │  dedup)       │     │              │
│ • Webhook    │     │               │     │               │     │ OntologyStore│
│ • Cron       │     │               │     │               │     │              │
│ • Web sync   │     │               │     │               │     │              │
│ • Agent FX   │     │               │     │               │     │              │
└──────────────┘     └──────────────┘     └──────────────┘     └──────┬───────┘
                                                                       │
                              ┌────────────────────────────────────────┘
                              ▼
                     ┌──────────────────┐
                     │   INDEX UPDATE   │
                     │  (async, batch)  │
                     │ • EmbeddingIndex │
                     │ • Graph indexes  │
                     │ • Constraint re- │
                     │   evaluation     │
                     │ • Goal progress  │
                     └────────┬─────────┘
                              │
                              ▼
                     ┌──────────────────┐
                     │   REASONING      │
                     │  (on-demand)     │
                     │ • Constraint chk │
                     │ • Goal projection│
                     │ • Conflict detect│
                     │ • Simulation     │
                     │ • Scheduling     │
                     └──────────────────┘
```

---

## 3. Data Model

### 3.1 Entity Types (8 Universal Types)

| Type | Purpose | Key Fields |
|------|---------|------------|
| **User** | The person | name, email, timezone, birth_date, federation_id |
| **Metric** | Any measurable value | metric_name, value, unit, recorded_at, source |
| **Activity** | Any action/event with duration | name, category, intensity, start/end, calories, METs |
| **Goal** | Target state with deadline | name, category, target_value/unit, deadline, priority, milestones |
| **Constraint** | Hard/soft limitation | domain, severity, rules, valid_from/to, applies_now() |
| **Preference** | Behavioral preference | domain, key, value, strength (0-1), exceptions |
| **Contact** | Person in user's network | name, closeness (0-1), relationship_type, tags |
| **Skill** | Capability with proficiency | name, category, proficiency (1-5), prerequisites |

### 3.2 Relationship Types (10 Universal Types)

| Type | Source → Target | Cardinality | Use Case |
|------|-----------------|-------------|----------|
| `HAS_METRIC` | User → Metric | 1:many (temporal) | Track all measurements |
| `HAS_GOAL` | User → Goal | 1:many (active subset) | Active goals |
| `PREFERS` | User → Preference | 1:many | Preferences |
| `CONSTRAINED_BY` | User/Activity/Goal → Constraint | 1:many | Limitations |
| `PERFORMED` | User → Activity | 1:many (temporal log) | Action history |
| `TRACKS` | Goal → Metric/Activity | many:many | Progress tracking |
| `RELATED_TO` | Any → Any | many:many | Semantic links |
| `CONFLICTS_WITH` | Any → Any | many:many | Mutex constraints |
| `PREREQUISITE_OF` | Skill → Skill | many:many | Dependency graph |
| `SCHEDULES` | Event → Activity/Goal | 1:1 | Calendar binding |

### 3.3 Universal Entity Fields

```python
class Entity(BaseModel):
    id: str                              # ent_<uuid12>
    type: EntityType                     # USER, METRIC, ACTIVITY, GOAL, CONSTRAINT, PREFERENCE, CONTACT, SKILL
    created_at: datetime
    updated_at: datetime
    
    # Identity & Federation
    federation_id: str | None = None     # north://user@domain (future sync)
    
    # Temporal (both models)
    valid_from: datetime | None = None   # interval start
    valid_until: datetime | None = None  # interval end (None = point-in-time)
    
    # Provenance (always)
    source: SourceType = SourceType.MANUAL
    source_ref: str | None = None        # "apple_health://workout/123", "llm:gpt-4o/extract-2026-07-26"
    extracted_at: datetime
    confidence: float = 1.0              # 0.0-1.0
    
    # Privacy (future)
    privacy: PrivacyLevel = PrivacyLevel.PUBLIC  # PUBLIC, PRIVATE, SENSITIVE
    
    # External ontology links
    external_mappings: list[ExternalMapping] = []
    
    # Domain-specific data (validated by subtype)
    data: dict[str, Any] = {}
```

### 3.4 Relationship Fields

```python
class Relationship(BaseModel):
    id: str                              # rel_<uuid12>
    type: RelationshipType
    source_id: str
    target_id: str
    
    # Provenance (same as entity)
    source: SourceType = SourceType.MANUAL
    source_ref: str | None = None
    extracted_at: datetime
    confidence: float = 1.0
    privacy: PrivacyLevel = PrivacyLevel.PUBLIC
    
    # For inferred relationships
    inference_rule: str | None = None    # "recovery_constraint_from_intensity"
    
    # Optional properties
    properties: dict[str, Any] = {}
```

### 3.5 Provenance Enum

```python
class SourceType(str, Enum):
    MANUAL = "manual"
    USER_PROMPT = "user_prompt"
    TOOL_OUTPUT = "tool_output"
    DEVICE_SYNC = "device_sync"
    WEB_SYNC = "web_sync"
    CRON = "cron"
    WEBHOOK = "webhook"
    EXTERNAL_ONTOLOGY = "external_ontology"
    LLM_INFERRED = "llm_inferred"
    AGENT_SIDE_EFFECT = "agent_side_effect"
```

---

## 4. Ingestion Pipeline

### 4.1 Pipeline Stages

```
SOURCE → PARSER → VALIDATION → STORAGE → INDEX → REASONING TRIGGERS
```

### 4.2 Sources

| Source | Example | SourceType |
|--------|---------|------------|
| User chat | "I weigh 78kg" | USER_PROMPT |
| Tool output | `bash: withings export` | TOOL_OUTPUT |
| Device webhook | Whoop sleep data | DEVICE_SYNC / WEBHOOK |
| Scheduled cron | Daily weight pull | CRON |
| Web sync | Plaid transactions | WEB_SYNC |
| External ontology | FHIR code update | EXTERNAL_ONTOLOGY |
| Agent side-effect | Coder creates skill | AGENT_SIDE_EFFECT |

### 4.3 OntologyParser (LLM)

**Input:** Raw content + source metadata  
**Prompt:** Schema-aware extraction (entities + relationships)  
**Output:** Structured JSON with confidence per item

```python
class OntologyParser:
    async def parse(
        self,
        content: str,
        source_type: SourceType,
        source_ref: str,
        context: dict | None = None
    ) -> ParsedOntology:
        """
        Returns:
            entities: list[EntityDraft]  # type, data, confidence
            relationships: list[RelationshipDraft]  # type, source, target, confidence
            warnings: list[str]  # contradictions, low confidence
        """
```

**Parser Prompt (simplified):**
```
SCHEMA: EntityType {User, Metric, Activity, Goal, Constraint, Preference, Contact, Skill}
        RelationshipType {HAS_METRIC, HAS_GOAL, PREFERS, CONSTRAINED_BY, PERFORMED, 
                         TRACKS, RELATED_TO, CONFLICTS_WITH, PREREQUISITE_OF, SCHEDULES}

SOURCE: {source_type} - {source_ref}
CONTENT: {raw_content}

EXTRACT all entities and relationships. Return JSON:
{
  "entities": [{"type": "Metric", "data": {"metric_name": "weight", "value": 78, "unit": "kg"}, "confidence": 0.99}],
  "relationships": [{"type": "HAS_METRIC", "source": "user_kushagra", "target": "<entity_id>", "confidence": 0.99}]
}
```

### 4.4 Validation

1. **Schema validation** — Pydantic model validation per entity type
2. **Relationship validity** — `validate_relationship(source_type, target_type)` per rules table
3. **Deduplication** — Semantic similarity (embedding) + key-field matching (metric_name + recorded_at)
4. **Conflict detection** — New fact contradicts existing high-confidence fact → flag

### 4.5 Storage

`OntologyStore` — SQLite with JSON columns + graph indexes

```sql
-- Entities
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    data TEXT NOT NULL,           -- JSON
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    federation_id TEXT,
    valid_from TEXT,
    valid_until TEXT,
    source TEXT NOT NULL,
    source_ref TEXT,
    extracted_at TEXT NOT NULL,
    confidence REAL NOT NULL,
    privacy TEXT NOT NULL
);

-- Relationships
CREATE TABLE relationships (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    properties TEXT NOT NULL,     -- JSON
    created_at TEXT NOT NULL,
    confidence REAL NOT NULL,
    inferred INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    extracted_at TEXT NOT NULL,
    privacy TEXT NOT NULL,
    inference_rule TEXT,
    FOREIGN KEY (source_id) REFERENCES entities(id),
    FOREIGN KEY (target_id) REFERENCES entities(id)
);

-- Indexes
CREATE INDEX idx_entities_type ON entities(type);
CREATE INDEX idx_entities_federation ON entities(federation_id);
CREATE INDEX idx_entities_valid ON entities(valid_from, valid_until);
CREATE INDEX idx_rels_source ON relationships(source_id);
CREATE INDEX idx_rels_target ON relationships(target_id);
CREATE INDEX idx_rels_type ON relationships(type);
```

---

## 5. Reasoning Engine

### 5.1 Core Capabilities

| Capability | Method | Output |
|------------|--------|--------|
| **Constraint Validation** | Rule evaluation over graph | `ValidationResult {valid, violations[], warnings[]}` |
| **Goal Projection** | Linear/regression on metric history | `Projection {on_track, projected_date, gap, levers[]}` |
| **Conflict Detection** | Graph pattern matching | `Conflict {type, entities[], severity, options[]}` |
| **Simulation** | Temporary graph mutation + re-eval | `SimulationResult {outcomes[], risks[], tradeoffs[]}` |
| **Scheduling** | OR-Tools CP-SAT | `Schedule {activities[], constraints_satisfied, score}` |

### 5.2 Constraint Validation

```python
class Reasoner:
    def check_constraints(
        self,
        proposed_activities: list[Activity],
        window: TimeRange
    ) -> ValidationResult:
        """
        1. Load all active constraints for user
        2. For each proposed activity:
           - Check HARD constraints (medical, legal, safety, resource ceilings)
           - Check SOFT constraints (preferences, recovery windows)
        3. Check cross-activity conflicts (recovery overlap, resource double-booking)
        4. Return: valid=True/False + violations + warnings + recommendation
        """
```

**Constraint Priority Matrix (for auto-resolution):**
```
MEDICAL > LEGAL > SAFETY > RESOURCE_CEILING > GOAL > PREFERENCE
```

### 5.3 Goal Projection

```python
def project_goal(self, goal_id: str) -> Projection:
    """
    1. Load goal + tracked metrics
    2. Fit trend (linear / exponential / piecewise)
    3. Project to deadline
    4. Compute: on_track (bool), projected_value, gap, required_rate
    5. Identify levers: [increase activity X, reduce spend Y, extend deadline]
    """
```

### 5.4 Conflict Detection

```python
def detect_conflicts(self, window: TimeRange) -> list[Conflict]:
    """
    Patterns detected:
    - HARD constraint violation (medical, legal)
    - Recovery overlap (high-intensity activities < min_recovery_hours apart)
    - Resource double-booking (time, calories, money, attention)
    - Goal conflict (competing for same resource)
    - Preference violation (soft, with penalty cost)
    """
```

### 5.5 Simulation (What-If)

```python
def simulate(
    self,
    proposed_activities: list[Activity],
    proposed_constraints: list[Constraint] = [],
    proposed_goals: list[Goal] = []
) -> SimulationResult:
    """
    1. Create temporary graph copy
    2. Apply proposed changes
    3. Re-run: constraint check, goal projections, conflict detection
    4. Return: outcomes per goal, risks, tradeoffs, recommendation
    """
```

### 5.6 Constraint Scheduling

```python
class ConstraintScheduler:
    def schedule_week(
        self,
        goals: list[Goal],
        constraints: list[Constraint],
        fixed_events: list[Event],
        preferences: list[Preference]
    ) -> Schedule:
        """
        CP-SAT model:
        Variables: activity_start_time[activity_id] ∈ [week_start, week_end]
        Constraints:
          - fixed_events fixed
          - recovery windows between high-intensity
          - resource ceilings (time, calories, money, attention)
          - preference windows (soft, with penalty cost)
          - goal progress requirements (min sessions/week per goal)
        Objective: maximize goal progress + preference satisfaction
        """
```

---

## 6. Conflict Resolution (Autonomy-Aware)

### 6.1 Autonomy Levels

```python
class AutonomyLevel(str, Enum):
    LOW = "ask_everything"        # Every conflict → human
    MEDIUM = "ask_hard_conflicts" # Hard vs Hard → human; Soft auto
    HIGH = "resolve_soft_only"    # Soft auto; Hard → human
    MAX = "resolve_all"           # All auto with logged rationale
```

### 6.2 Resolution Behavior

| Conflict Type | LOW | MEDIUM | HIGH | MAX |
|---------------|-----|--------|------|-----|
| Hard vs Hard | Human | Human | Human | Auto (priority) |
| Hard vs Soft | Human | Human | Human | Auto (priority) |
| Soft vs Soft | Human | Auto (notify) | Auto | Auto |
| Goal vs Goal | Human | Human | Human | Auto (priority) |

**Every resolution creates:**
```python
Resolution {
    conflict_id: str,
    resolution_type: HUMAN | AUTO,
    chosen_option: str,
    authority: "human" | "autonomy_engine",
    confidence: float,
    rationale: str,
    timestamp: datetime
}
```

**Learning:** Repeated human choices → propose new constraint with `source=LLM_INFERRED`

---

## 7. Identity & Federation

### 7.1 Current: Single Person
```python
User {
    id: "user_kushagra",
    federation_id: "north://kushagra@personal",
    peer_endpoints: []  # future
}
```

### 7.2 Future: Peer-to-Peer Sync
- `federation_id` = DID or `north://user@domain`
- `peer_endpoints` = list of trusted North instances
- Sync protocol: CRDT or operational transform for graph merge
- Shared entities: Calendar Events, FinancialGoals, Constraints (quiet hours)
- Conflict resolution: same autonomy model

---

## 8. Temporal Model

### 8.1 Both Point-in-Time + Intervals

| Model | Entity Types | Query API |
|-------|--------------|-----------|
| **Point-in-time** | Metric, Goal (snapshot) | `get_state(at: datetime)` |
| **Interval** | Activity, WorkSession, Sleep, Event | `get_history(range: TimeRange)` |

### 8.2 Unified Query
```python
def get_state(at: datetime) -> WorldState:
    """All active intervals at `at` + latest metric snapshots ≤ `at`"""

def get_history(range: TimeRange) -> Timeline:
    """Intervals overlapping range + metric snapshots in range"""

def get_trend(metric_name: str, window: Duration) -> Trend:
    """Regression on metric points in window"""
```

---

## 9. Provenance

### 9.1 Every Entity/Relationship Carries

```python
source: SourceType                    # MANUAL, DEVICE_SYNC, LLM_INFERRED, etc.
source_ref: str                       # "withings://scale/user_123/measurement_456"
extracted_at: datetime                # When ingested
confidence: float                     # 1.0 = manual, <1.0 = LLM/device
inference_rule: str | None            # For derived relationships
```

### 9.2 Ingestion Pipeline Mirrors FactStore

| Stage | FactStore | OntologyStore |
|-------|-----------|---------------|
| Parser | ContextInjector (routes to 3 docs) | OntologyParser (LLM + schema) |
| Unit | Single fact (sentence) | Typed entity + relationships |
| Provenance | Implicit (source doc) | Explicit (SourceType + ref + confidence) |
| Dedup | Cosine similarity > 0.85 | Semantic + key-field |
| Recall | Vector similarity | Vector + graph + structured query |
| Ledger | MANUAL_INJECTION | ONTOLOGY_INGESTION |

---

## 10. Privacy Boundary

### 10.1 Current: None (All Local)
- Full graph available to LLM for reasoning
- No redaction, no "private" flags
- Maximum reasoning power

### 10.2 Future: Granular Boundary
```python
class PrivacyLevel(str, Enum):
    PUBLIC = "public"        # Full LLM context, exportable
    PRIVATE = "private"      # Local reasoning only, excluded from prompts
    SENSITIVE = "sensitive"  # Encrypted at rest, hard constraints only
```

---

## 11. External Ontologies

### 11.1 Live Access + Local Mirror

| Standard | Domain | North Mapping |
|----------|--------|---------------|
| **schema.org** | General | Activity, Event, Contact, Skill, Organization |
| **FHIR (R4)** | Health | BodyMetric (Observation), Constraint (Condition), Activity (Procedure) |
| **FIBO** | Finance | FinancialMetric, FinancialGoal, Transaction, Account |
| **O*NET / HR-XML** | Career | Skill (O*NET code), CareerGoal, WorkSession |
| **FOAF / vCard** | Social | Contact, Relationship |

### 11.2 ExternalOntologyClient
```python
class ExternalOntologyClient:
    async def search(self, standard: str, query: str) -> list[Mapping]
    async def get_entity(self, standard: str, uri: str) -> ExternalEntity
    async def sync_mappings(self, standard: str) -> SyncResult
    
    # Local mirror in OntologyStore:
    # ExternalOntologyMapping {standard, uri, local_entity_id, mapped_fields, last_synced}
```

---

## 12. Integration Points

### 12.1 Existing Components Extended

| Component | Extension |
|-----------|-----------|
| `memory/injection.py` | `ContextInjector` → calls `OntologyIngestion` for structured content |
| `orchestrator/north_star.py` | Adds constraint validation before task execution |
| `agents/agentic_llm_agent.py` | Injects relevant subgraph into system prompt |
| `ledger/` | New `ONTOLOGY_INGESTION` source, `ONTOLOGY_REASONING` action |
| `cron/` | Device sync, external refresh, dedup jobs |

### 12.2 New Skills (Agent-Callable)

| Skill | Trigger | Purpose |
|-------|---------|---------|
| `ontology-ingest` | Structured data from user/tool/device | Parse → validate → store |
| `ontology-query` | Complex graph questions | "What metrics track my fitness goal?" |
| `ontology-plan` | "Plan my week" | Constraint-aware scheduling |
| `ontology-simulate` | "What if I add X?" | What-if simulation |

### 12.3 New Tools (Agent-Callable)

| Tool | Signature | Purpose |
|------|-----------|---------|
| `ontology_ingest` | `ingest(content, source_type, source_ref)` | Programmatic ingestion |
| `ontology_query` | `query(cypher_like_or_structured)` | Graph traversal |
| `ontology_simulate` | `simulate(activities, constraints, goals)` | What-if |

---

## 13. Storage & Performance

### 13.1 SQLite Configuration
- WAL mode for concurrent reads
- `PRAGMA cache_size = -32768` (32MB)
- `PRAGMA mmap_size = 268435456` (256MB)
- Connection pooling via `utils.db.open_db_connection`

### 13.2 Indexing Strategy
| Index | Purpose |
|-------|---------|
| `idx_entities_type` | Type-filtered queries |
| `idx_entities_federation` | Federation sync |
| `idx_entities_valid` | Temporal queries |
| `idx_rels_source/target/type` | Graph traversal |
| EmbeddingIndex | Semantic recall (paragraph-level) |

### 13.3 Expected Scale
- Entities: ~1,000–10,000 (personal lifetime)
- Relationships: ~5,000–50,000
- Queries: <10ms for graph traversal, <50ms for CP-SAT scheduling

---

## 14. Migration Strategy

### 14.1 From Existing Memory
1. **North Stars** → `Goal` entities (category=life, priority=1)
2. **Judgement Rules** → `Constraint` (severity=soft) + `Preference` (strength=0.8)
3. **User Facts** → `Metric` (if numeric) or `Preference` (if behavioral)
4. **Vector Facts** → Remain in FactStore; new ingestion feeds both

### 14.2 Backward Compatibility
- Existing `ContextInjector` unchanged for free-text
- New structured ingestion *adds* to ontology layer
- Agents see merged context: semantic facts + ontology subgraph

---

## 15. Open Questions (Resolved in Design)

| Question | Decision |
|----------|----------|
| Conflict resolution | Human-in-the-loop, controlled by autonomy flag |
| Identity | Single person now; `federation_id` for future P2P |
| Temporal model | Both point-in-time (metrics) + intervals (activities) |
| Provenance | Always tracked; mirrors FactStore pattern; LLM-parsed ingestion |
| Privacy boundary | None for now; `PrivacyLevel` enum for future |
| External ontologies | Live access + local mirror; FHIR, schema.org, FIBO, O*NET |
| Schema evolution | Flexible (JSON + Pydantic); version field on entities |

---

## 16. Implementation Phases

| Phase | Deliverable | Timeline |
|-------|-------------|----------|
| **1. Foundation** | `ontology/` package: entities, relationships, store, basic CRUD, tests | Week 1-2 |
| **2. Ingestion** | Parser, ingestion pipeline, device sync cron, injection integration | Week 2-3 |
| **3. Reasoning** | Constraint checker, goal projector, conflict detector, simulator | Week 3-4 |
| **4. Scheduling** | CP-SAT scheduler, weekly planning skill | Week 4-5 |
| **5. Agent Integration** | Subgraph injection, ontology skills/tools, autonomy-aware resolution | Week 5-6 |
| **6. External & Polish** | FHIR/FIBO clients, TUI viz, migration from memory, docs | Week 6-7 |

---

## 17. Appendix: Key Files

```
north/ontology/
├── __init__.py
├── models.py              # SourceType, PrivacyLevel, EntityType, RelationshipType
├── entities.py            # User, Metric, Activity, Goal, Constraint, Preference, Contact, Skill
├── relationships.py       # Relationship, validation rules, convenience constructors
├── store.py               # OntologyStore (SQLite + graph indexes)
├── parser.py              # OntologyParser (LLM extraction)
├── ingestion.py           # OntologyIngestion (orchestration)
├── reasoner.py            # Reasoner (constraints, goals, conflicts, simulation)
├── scheduler.py           # ConstraintScheduler (OR-Tools CP-SAT)
├── external.py            # ExternalOntologyClient (FHIR, schema.org, FIBO, O*NET)
├── query.py               # GraphQuery, DecisionContext
└── tests/
    ├── test_entities.py
    ├── test_relationships.py
    ├── test_store.py
    ├── test_parser.py
    ├── test_reasoner.py
    └── test_scheduler.py
```

---

**End of Specification**