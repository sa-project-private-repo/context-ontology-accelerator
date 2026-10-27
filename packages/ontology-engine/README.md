# Ontology Engine

- **Owner**: TBD
- **LLD**: TBD
- **Status**: In development

Automated induction of OWL 2 ontologies from relational schemas and
unstructured documents. Grounds against foundational ontologies
(Schema.org, Dublin Core, PROV-O, FOAF, FIBO) via vector similarity
(Amazon Bedrock Cohere Embed v4). Uses large language models
(Amazon Bedrock Claude) for concept extraction, alignment, and
rigor-style generate/judge induction. Persists ontologies and
embeddings in Amazon Neptune Analytics (graph + HNSW vector index) or
Neptune Database + OpenSearch Serverless.

**Data model:** see [`docs/ontology-data-model.md`](docs/ontology-data-model.md)
for the RDF/OWL terminology (including reification and RDF-star), how a
relational schema is represented, where metadata lives, and the tradeoffs of
that design.

## Layout

    packages/ontology-engine/
    ├── src/coa_ontology/
    │   ├── main.py                      FastAPI service entry
    │   ├── induce_catalog.py            induction driven by data-catalog datasources
    │   ├── proposals.py                 proposal review workflow
    │   ├── dynamo_store.py              DynamoDB single-table store
    │   ├── bedrock_embeddings.py        direct Bedrock embedding client
    │   ├── abox/                        domain ABox from documents (see below)
    │   ├── studio/                      Ontology designer proposal drafts (see below)
    │   ├── catalog/                     ontology and embedding CRUD
    │   ├── inducer/                     induction pipelines
    │   │   ├── routers/                 /induce and /induce/document endpoints
    │   │   ├── services/                pipeline, LLM, embedding clients
    │   │   └── strategies/              pluggable induction strategies
    │   │       ├── table_to_ontology.py embedding-based matching
    │   │       └── rigor_ontology.py    RIGOR Gen-LLM + Judge-LLM
    │   └── validation/                  three-tier validation (HermiT, OntoQA, OoPS!)
    ├── scripts/
    │   └── live_induction_run.py        direct pipeline invocation without HTTP
    ├── demo/
    │   ├── setup.py                     foundational ontology loading
    │   ├── demo.py                      interactive induction walkthrough
    │   ├── parse_ddl_to_config.py       SQL DDL → YAML parser (sqlglot)
    │   ├── stage_fixtures.py            stage parsed tables into the mock catalog
    │   ├── pc-insurance-ddl.sql         OMG P&C benchmark schema (13 tables, 65 columns)
    │   └── pc-insurance-tables.yaml     parser output ready for staging
    ├── neptune-config/
    │   └── p8-config-vector.json        Neptune Analytics graph config
    └── tests/
        ├── unit/                        mocked, no live AWS (pytest.mark.unit)
        ├── integ/                       require live AWS (pytest.mark.integ)
        └── fixtures/
            └── data-catalog/            OpenMetadata-compatible mock catalog service

## Induction strategies

`table_to_ontology` (default) — embed each column name, vector-search
against grounding-ontology class and property embeddings, emit matches
at configurable similarity thresholds. One OWL class per table; columns
become datatype properties (object properties for foreign keys).

`rigor_ontology` — implements
[Retrieval-Augmented Generation of Ontologies from Relational
Databases](https://arxiv.org/abs/2506.01232) (Nayyeri et al., 2025). For
each table in FK topological order, retrieve related classes and
properties from the growing core ontology via embedding similarity,
call a generator LLM to produce an OWL fragment (Turtle), then call a
judge LLM to review and refine. Merge the refined fragment into the
core ontology. Finishes with a novelty check that separates matched
from novel terms.

## Unstructured induction (v0)

Induces an OWL 2 ontology directly from an unstructured lexical knowledge
graph in Neptune Analytics (entities, facts, and topics produced by the
document-ingestion pipeline) rather than from a relational schema. The
backend services live under `inducer/unstructured/`; the API + orchestration
wiring exposes them as a job-based HTTP endpoint that mirrors the structured
`/induce` surface.

### Calling the API

Start a job (returns `202 Accepted` + a `job_id`):

    POST /induce/unstructured/
    {
        "namespace": "default",
        "name": "my-ontology",
        "graph_arn": "arn:aws:neptune-graph:us-east-1:<account>:graph/<graph-id>",
        "ontology_uri_prefix": "http://coa.amazon.com/namespace/default/ontology/my-ontology",
        "description_mode": "schema",
        "include_individuals": false,
        "entailment_enabled": false
    }

Poll and list jobs:

    GET /induce/unstructured/jobs            # list jobs in a namespace
    GET /induce/unstructured/jobs/{job_id}   # poll a single job

On success the worker writes an `OntologyProposals` record tagged
`source_type=UNSTRUCTURED`. Review and accept it through the existing,
source-type-agnostic proposal endpoint:

    POST /ontology/proposals/{id}/accept

### Stage progression

A job moves through these statuses (per Requirement 2.4):

    pending → querying_neptune → sampling → generating_descriptions
        → resolving_iris → applying_rules → entailment → serializing
        → storing → completed → failed

`applying_rules` absorbs the LLD's `aligning` and `proposing_axioms` stages
(the class normalizer's `skos:closeMatch` bands are the alignment surface area
v0 emits). `entailment` is reported for API-contract stability but is a
pass-through in v0 (see below). The LLD's `validating` stage is omitted from
v0. The orchestrator emits stages through `serializing`; the worker thread
owns the terminal `storing` / `completed` / `failed` transitions.

### v0 limitations

v0 is intentionally incomplete in four areas. These are known gaps, not bugs;
the first three are also surfaced at runtime in the job's
`InductionReport.warnings` list so they show up in the API response, not just
here. Each is handed off to a deferred task in the parent spec
(`.kiro/specs/unstructured-ontology-induction/`).

1. **Sparse `owl:DatatypeProperty` output.** ~84% of `__Fact__` nodes on the
   dev graph encode the complement inline in the `value` string with no
   `__OBJECT__` edge, so Rule 4 (`induce_datatype_properties`) sees no SPC
   facts and emits few or zero datatype properties. → parent spec **Task 12B**
   (predicate-type extraction from inline complements).
2. **No cross-run normalizer persistence.** The `ClassNormalizer` rebuilds an
   in-memory class-embedding index on every run, so the LLD's "subsequent
   batches reuse existing IRIs" convergence story does not hold yet. → parent
   spec **Task 10A** (persistent class embedding store).
3. **No hierarchy axioms.** The `entailment` stage is a pass-through and emits
   zero `rdfs:subClassOf` / `owl:equivalentClass` / `owl:disjointWith` axioms.
   The only alignment triples come from the class normalizer's
   `skos:closeMatch` bands. → parent spec **Task 13** (OntoEKG-style entailment
   engine).
4. **No SHACL / HermiT validation.** Tier 1/2/3 validation (HermiT, OntoQA,
   OoPS!) is not invoked, so proposals are stored without conformance checks.
   The validation framework (`validation/`) is its own subsystem, wired in
   separately rather than by a single deferred induction task.

### Deferred work

Pointers to the parent spec's deferred tasks that close these gaps:

| Parent-spec task | Addresses |
|---|---|
| Task 10A | Persistent class embedding store → cross-run IRI convergence |
| Task 12B | Predicate-type extraction → richer `owl:DatatypeProperty` output |
| Task 13  | OntoEKG-style entailment engine → inferred class hierarchy axioms |

See `.kiro/specs/unstructured-ontology-induction/` for the parent spec and
`.kiro/specs/unstructured-ontology-induction-api/` for this API wiring spec.

## Domain ABox

After a domain ontology version is deployed, `abox/` rebuilds the namespace's
domain ABox from the documents it already ingested: typed instances,
relations, attributes and their evidence chunks, in the named graph
`{base}/_abox/{ns}`. User-facing behavior: "Domain data" in
`external-docs/content/ontologies.md`.

| Module | Role |
|---|---|
| `abox/model.py` | Vocabulary (`urn:coa:vocab#`), `norm` / `norm_key`, deterministic instance, evidence and free-attribute IRIs, record types |
| `abox/tbox.py` | The deployed version's TBox from its snapshot `ontology.ttl`: classes, properties, ancestors, domain/range fit |
| `abox/store.py` | DynamoDB rows `ABOXREQ` / `ABOXLOCK` / `ABOX` with lock-fenced writes, graph IRIs, S3 keys |
| `abox/mapping.py` | The namespace's mapping table (`ABOXMAP#…`): manual overrides, `stale` / `ignored` status, revision hash |
| `abox/matcher.py` | Automatic mapping: embedding-ranked candidates, then one batched LLM choice |
| `abox/lexical.py` | Reads the graphrag lexical graph over openCypher, with chunk provenance, current Sources only |
| `abox/extract.py` | Re-extraction for classes the mapping left empty: nearest AOSS chunks, Bedrock JSON, S3 cache |
| `abox/assemble.py` | Deterministic, TBox-validated assembly of the ABox graph and its counts |
| `abox/build.py` | Coalesced, locked, heartbeated builds with a fenced staging-to-live `MOVE` swap; `clear_abox`, `drop_graphs` |
| `abox/queries.py` | SPARQL reads for the API: instance list and bounded instance detail |
| `abox/router.py` | Public routes under `/abox` and the VPC-only `POST /internal/abox/requests` |

Environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `ABOX_STALE_SECONDS` | `1800` | A build lock without a heartbeat for this long is stale and can be taken over |
| `ABOX_LEXICAL_PAGE` | `20000` | Fact ids read per lexical page |
| `ABOX_LEXICAL_BATCH` | `500` | Fact ids per openCypher detail request |
| `ABOX_MAPPING_TOP_K` | `5` | Candidate ontology terms per mapping item |
| `ABOX_MAPPING_MODEL_ID` | `LLM_MODEL_ID`, else `us.anthropic.claude-sonnet-5` | Mapping judge model |
| `ABOX_REEXTRACT_MODEL_ID` | `LLM_MODEL_ID`, else `us.anthropic.claude-sonnet-5` | Re-extraction model |
| `ABOX_REEXTRACT_TOP_K` | `20` | Nearest chunks per target class |
| `ABOX_REEXTRACT_MAX_CHUNKS` | `300` | Chunks re-extracted per build |
| `ABOX_LLM_TEMPERATURE` | unset (no temperature is sent) | Temperature in [0, 1] for the mapping and re-extraction calls; newer Claude models reject a non-default value |
| `ABOX_LLM_MAX_TOKENS` | `8000` | Output-token cap for the mapping and re-extraction calls; a mapping batch whose reply still hits it is split in half. Kept below what Bedrock can generate within the client's 90 s read timeout, so a long reply truncates and splits instead of timing out |

## Ontology designer

`studio/` holds the editable draft of a proposal (the Ontology designer):
canonical N-Triples revisions under `proposals/{ns}/{id}/studio/` and a `studio`
map on the proposal row, advanced by a conditional write on `studio.revision`.
Accepting a draft publishes it in `publish_version`'s replace mode. User-facing
behavior: "Editing a proposal in the Ontology designer" in
`external-docs/content/ontologies.md`.

| Module | Role |
|---|---|
| `studio/iris.py` | The namespace's IRI space; IRIs minted from labels with the inducers' rules; the datatypes a range may name |
| `studio/ops.py` | The 14 typed change ops, their validation, and `restore_triples` (the exact inverse, never accepted from a client) |
| `studio/draft.py` | S3 keys, revision claim (`If-None-Match`) and conditional commit, opening a draft, the merge a draft and a rebase start from |
| `studio/changes.py` | Change sets, dry runs, undo, redo and history |
| `studio/rebase.py` | Rebuilding a draft on the newest version and replaying its user ops |
| `studio/graph_view.py` | The graph (full or focus view) with ABox badges, term detail, term search |
| `studio/router.py` | Routes under `/ontology/proposals/{proposal_id}/studio`, and the accept preview |
| `studio/chat_store.py` | The assistant's turns: the per-proposal turn lease, the chat row, the JSONL event files, sweeping a dead turn |
| `studio/changesets.py` | The change sets the assistant proposes: dry run on proposal, preview with marks, apply (all or some ops, in the same transaction as the revision), discard |
| `studio/agent_tools.py` | The six tools the agent reads and proposes with; every result is JSON in `<data>`, with `<` escaped |
| `studio/agent.py` | The Strands agent turn: system prompt, hooks that write the events, the tool and time budgets, conversation memory |

The existing proposal routes meet the draft in `proposals.py`: `/update` and
`/upload-url` answer 409 `StudioDraftOpen` for a proposal open in the designer,
`/validate` reads the draft's current revision, `/repair-datatypes` repairs the
draft as a change set of `replace_datatype` ops, and `/accept` publishes the
revision named by `expectedRevision` (`PublishRequest.mode = "replace"`).

The assistant runs in this process: `POST .../studio/messages` takes the
proposal's turn lease (`STUDIOTURN#{proposalId}` under `NAMESPACE#{ns}`), answers
202 and runs a Strands agent on a daemon thread. Its steps go to
`proposals/{ns}/{id}/studio/chat/{turnId}.jsonl`, committed by `last_seq` on the
`STUDIOCHAT` row of the proposal, and the page polls `GET .../studio/events`.
The agent's only write is `propose_changes`, which stores a pending
`STUDIOCS#{changeSetId}` row; the user applies it with
`POST .../changesets/{id}/apply`, which writes a revision with `source = "agent"`.
`count_active_jobs` counts live turns, so a namespace is not deleted under one.

Checks, queries and competency questions: `checks.py` computes the
structural findings of a revision and compares it with the source catalog, the
domain ABox, its mappings and the governed metrics (`GET .../studio/checks`).
`queries.py` runs the query console (`POST .../studio/queries`): draft SPARQL
reads the revision's own named graph `{base}/_studio/{ns}/{id}/r{revision}`,
loaded on first use and named by the proposal's `STUDIOGRAPH` row, plus the
document ABox; `query_safety.py` refuses queries that name graphs and injects
`FROM` itself, and keeps Cypher to the namespace's tenant labels. A newer revision marks the
older graphs superseded and drops them after a grace period
(`STUDIO_SUPERSEDED_GRACE_SECONDS`, 120 s), since another request may still read
them. Accept, cancel, reject and namespace deletion drop the draft graphs and
close the row, so a load still running cannot record into it. `questions.py` keeps the
namespace's competency questions (`STUDIOCQ#{id}` under `NAMESPACE#{ns}`); the
assistant checks one with `run_query` and `check_question`. Queries over the
database go to the data layer's `/sparql` instead, which runs them through
serve's VKG pipeline with the caller's grants.

Things to know when changing it:

- Every record under `ops/` stores the *resolved* ops: an `add_*` op carries the
  IRI it minted, so redo and rebase create that very term or fail, and never mint
  another.
- Revision numbers are claimed, not counted, so they can skip. A writer that loses
  the conditional commit leaves its record and files as an unreferenced gap; they
  are never deleted (except a lost concurrent open, which discards its own
  revision 0), and deleting the namespace purges `proposals/{ns}/`.
- Every write is refused with 409 `ProposalNotEditable` unless the proposal is
  `pending`, `updated` or `accept_failed`. Opening an existing draft returns it
  whatever the status.
- Errors are `HTTPException` with `detail = {code, message, ...}`. A stored change
  record that does not parse is a 500; only the ops a client sends to
  `POST .../changes` give 422.
- A chat event is visible only once the chat row's `last_seq` covers it: readers
  never return an event past the committed `last_seq` of its turn, so the S3 write
  that precedes the row update can never show a step twice. A turn whose lease
  went stale is swept to `failed` on the next read or message, and its worker
  stops at its next event (`TurnLost`).
- Applying a change set and committing its revision are one DynamoDB transaction
  (`draft.commit(also_ops=...)`), so a change set is applied at most once and never
  after a discard.

Environment variables (all optional, read once at start-up; these are the only
ones `studio/` reads):

| Variable | Default | Purpose |
|---|---|---|
| `STUDIO_GRAPH_MAX_CLASSES` | `150` | Above this many classes the graph API serves a focus view. The web app's **Show all classes** button assumes 150 |
| `STUDIO_GRAPH_MAX_FOCUS_NODES` | `150` | Classes in one focus view |
| `STUDIO_AGENT_MODEL_ID` | `LLM_MODEL_ID`, else `us.anthropic.claude-sonnet-5` | The assistant's Bedrock model (also its conversation summaries) |
| `STUDIO_AGENT_MAX_TOOL_CALLS` | `25` | Tool calls in one turn; further calls are cancelled so the assistant answers |
| `STUDIO_AGENT_TURN_SECONDS` | `300` | Time budget of one turn; the agent is cancelled at it |
| `STUDIO_QUERY_SECONDS` | `20` | Time budget of one console query (Neptune `queryTimeout` hint and client timeout); keep it under API Gateway's 29 s |
| `STUDIO_TURN_STALE_SECONDS` | `90` | A turn lease without a heartbeat for this long is a dead worker's (heartbeats every 20 s) |

## Backends

Selected via `WORKBENCH_BACKEND`:

- `na_only` — Neptune Analytics for both graph storage and HNSW vector
  search. Simplest deployment.
- `opensearch_neptune` — Neptune Database for graph, OpenSearch
  Serverless for vector search. Higher throughput, more moving parts.

> **⚠️ Tier-2 answerability depends on the backend.** The `coa:isMapped` marker
> that gates R2RML-mapped classes into the structured-query path (NL→SPARQL /
> NL→SQL) is written **only by the `opensearch_neptune` (NDB) backend** at
> ingest. The `na_only` (Neptune Analytics) backend currently **no-ops** the
> marker write, so **every class on an NA-backed namespace is unmapped → all
> Tier-2 structured queries fall back to Tier-3**. Use `opensearch_neptune` if
> you need structured querying. NA marker persistence is a known limitation
> (planned follow-up).

> **Ontology versions are deployed only on `opensearch_neptune`.** Accepting a
> proposal publishes an immutable ontology version; deploying a version (and
> rolling back to an older one) requires `WORKBENCH_BACKEND=opensearch_neptune`.
> On `na_only` an accept still publishes the version, but its automatic deploy is
> held (`auto_deploy_held`, reason `DEPLOY_FAILED_TO_START`), so the namespace
> keeps serving what it served before. `scripts/live_induction_run.py` therefore
> refuses `na_only`.

## Quick start

Copy `demo/.env.example` to `.env` in this package root, fill in your
AWS resource IDs, then from the monorepo root:

    uv run --package coa-ontology uvicorn \
        coa_ontology.main:app --port 8080

Load foundational ontologies and explore interactively:

    cd packages/ontology-engine/demo
    python setup.py
    python demo.py

For non-interactive induction against data-catalog-registered
datasources:

    python scripts/live_induction_run.py

## Tests

Unit tests (no live AWS required):

    uv run pytest packages/ontology-engine -m unit -v

Integration tests (require live AWS + data-catalog fixture running):

    # Terminal 1 — start the mock data-catalog
    cd packages/ontology-engine/tests/fixtures/data-catalog
    uvicorn app.main:app --port 8003

    # Terminal 2 — run integ tests
    uv run pytest packages/ontology-engine -m integ -v

The `tests/fixtures/data-catalog/` directory contains a standalone
OpenMetadata-compatible mock catalog, used by integration tests to
register schemas without touching production catalogs. See its
`README.md` for standalone use.

## Configuration

Runtime environment variables (see `demo/.env.example` for the complete
list):

| Variable | Purpose |
|---|---|
| `WORKBENCH_BACKEND` | `na_only` or `opensearch_neptune` |
| `NA_GRAPH_ID` | Neptune Analytics graph ID (na_only backend) |
| `NDB_ENDPOINT`, `OSS_ENDPOINT` | Neptune DB + OpenSearch (opensearch_neptune backend) |
| `DYNAMODB_TABLE` | Single-table store for jobs and proposals |
| `BEDROCK_REGION`, `BEDROCK_EMBED_MODEL_ID` | Embedding model config |
| `LLM_MODEL_ID`, `LLM_REGION` | LLM config (RIGOR strategy and document induction) |
| `DATA_CATALOG_URL` | URL of the data-catalog service (default `http://localhost:8003`) |

## References

- [OWL 2 Profiles](https://www.w3.org/TR/owl2-profiles/) (QL, RL, EL)
- [R2RML — RDB to RDF Mapping Language](https://www.w3.org/TR/r2rml/)
- [SKOS Simple Knowledge Organization System](https://www.w3.org/TR/skos-reference/)
- [Amazon Neptune Analytics Vector Search](https://docs.aws.amazon.com/neptune-analytics/latest/userguide/vector-search.html)
- [Cohere Embed v4 on Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html)
- Nayyeri et al., [RIGOR: Retrieval-Augmented Generation of Ontologies](https://arxiv.org/abs/2506.01232), 2025
- Sequeda et al., [A Benchmark to Understand the Role of Knowledge Graphs on LLM's Accuracy for QA on Enterprise SQL Databases](https://arxiv.org/abs/2311.07509), 2023
- Tartir et al., OntoQA: Metric-Based Ontology Quality Analysis, 2005
- Poveda-Villalón et al., OOPS! (OntOlogy Pitfall Scanner), 2014
