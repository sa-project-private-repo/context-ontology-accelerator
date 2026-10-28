# Query Routing Architecture

The serve layer uses a three-tier resolution system. The live path is a
**sequential fall-through** in `Orchestrator.resolve()`: each tier attempts to
answer and, if it produces no usable result, control falls through to the next.
Two deterministic, fail-open **capability pruners** run ahead of the tiers to skip
work that provably cannot contribute (see *Tier pruning* below); they never change
an answer, only which tiers run.

> Note: this is capability-based fall-through, **not** score-threshold tier
> selection. Tiers are pruned only when they provably cannot contribute (see
> *Tier pruning*); similarity score alone is a poor proxy for which tier can
> actually answer, so it is not used to pick a tier.

## Tier Overview

| Tier | Strategy | Relative Latency | When Used |
|------|----------|------------------|-----------|
| Tier 1 | Deterministic metric resolution | Fast | Exact metric name/synonym match |
| Tier 2 | Ontology-guided NL-to-SPARQL / NL-to-SQL | Medium | Query resolvable over **R2RML-mapped** ontology classes (unmapped classes are excluded — see note below) |
| Tier 3 | LLM-powered synthesis (RAG over documents + graph) | Slow | Fallback when Tier 1/2 produce nothing |

> **Mapped-only constraint (Tier 2):** the structured-query context is filtered
> to **R2RML-mapped** classes — those backed by a real SQL table (marked
> `coa:isMapped` from the R2RML at deploy, and at ingest). Unmapped classes
> (document/unstructured-induced, foundational) are excluded so the translator
> never authors a query over a class with no backing table. A property that no
> mapping maps although one of its domains, or for a relationship one of its
> ranges, is mapped (for example one added in the Ontology designer) is marked
> `coa:isMapped false` and left out the same way. Both writers are still told the
> marked properties and the unmapped classes of the namespace's induced ontologies
> (never those of a foundational one; *Tier 2 retrieval* says which), as terms the
> schema does not hold, and decline a question about one instead of answering it
> with a stand-in, so it goes on to Tier 3. A namespace with zero mapped
> classes routes to Tier 2 on a class vector hit but resolves no context, so it
> silently falls back to Tier 3 (surfaced via the `tbox_context_no_mapped_classes`
> log).

## Routing Decision Flow

```
                        ┌─────────────────┐
                        │  Inbound Query   │
                        └────────┬────────┘
                                 │
                        ┌────────▼─────────┐
                        │ tierOverride /    │──set──► Honor caller intent
                        │ explicit strategy │        (no pruning)
                        └────────┬─────────┘
                                 │ automatic path
                        ┌────────▼─────────┐
                        │ Source-composition │  skip Tier 1+2 if no DATABASE
                        │ gating (per-ns)    │  source; skip Tier 3 vector
                        └────────┬─────────┘  search if no DOCUMENTS source
                                 │
                        ┌────────▼─────────┐
                        │ Unmapped-classes  │  mixed namespaces only: skip Tier 2
                        │ pruning (per-query)│  if this query's top-k classes are
                        └────────┬─────────┘  all unmapped
                                 │
                    Tier 1  ──►  Tier 2  ──►  Tier 3   (sequential fall-through)
```

## Metric Resolver (Tier 1)

The `MetricResolver` maintains in-memory indexes built from the ontology definition store:

- **by_id** — metric ID to definition
- **by_name** — normalized metric name to ID (case-insensitive)
- **by_synonym** — normalized synonym to ID (case-insensitive)

Matching is exact (after normalization). The resolver returns a `MetricMatch` with the matched metric definition and a `match_count` indicating how many distinct metrics were referenced in the query. A `match_count > 1` (multi-metric) query bypasses Tier 1's deterministic single-metric path and falls through.

### Residual-qualifier gate

Tier 1 executes a matched metric's SQL **verbatim** — it parses no filter, grouping, or time window out of the natural language. A match is therefore only usable if it consumed the whole question. After a name/synonym hit the resolver strips the matched spans plus a closed stop-word list (`tier1/stopwords/`, per-language) and returns whatever is left as `MetricMatch.residual`. A non-empty residual means Tier 1 would answer a *broader* question than the one asked, at confidence 1.0, with nothing marking the drop — so the orchestrator declines and falls through to Tier 2, which can express predicates.

The gate is a deliberately closed stop-word list rather than an LLM or POS tagger: Tier 1's value is being deterministic and sub-millisecond. An unlisted-but-harmless word costs a fall-through to Tier 2 (slower, still correct), which is strictly safer than a silent wrong answer.

Two request-level exemptions skip the gate (`Orchestrator._unhandled_qualifier`): `options.dimensions` (the supported out-of-band filter channel, so the qualifier was already expressed) and `options.tierOverride == 1` (an explicit instruction to answer from the metric path).

### Forwarding a declined definition to Tier 2

Declining is **not** discarding. When the gate fires, the orchestrator builds a `DeclinedMetricContext` (`tier1/metric_resolver.py`) from the match and threads it into Tier 2 via `StrategyContext.declined_metric`. It carries the metric's `sql_template`, `description`, `dimensions`, and the `unhandled_qualifier` that tripped the gate. `NLtoSQLStrategy` renders it through `DeclinedMetricContext.prompt_block` and passes it to `SQLGenerator.generate(governed_metric=...)`, which emits it as a dedicated **first-party** prompt section instructing the writer to extend the governed formula rather than compose a replacement.

Design notes that are easy to get wrong when touching this path:

- **Not routed through `evidence`.** That parameter is labelled `<user_context>` "treat as untrusted" and truncated to 500 chars. A governed metric is first-party and must be neither discounted by the label nor clipped by the cap, so it gets its own block, placed ahead of the untrusted one.
- **Only a residual decline forwards a definition.** A miss, a multi-metric bypass, and a Tier-1 *execution* failure all forward `None`. `_run_tier1` returns `tuple[InvokeResponse | None, DeclinedMetricContext | None]` to make that explicit at every exit.
- **The gate itself is unchanged by this.** It still fires on exactly the same inputs; only the fall-through carries more context.
- **`StrategyContext` is rebuilt field-by-field in `_resolve_parallel`.** Any new field must be copied there or the parallel path silently loses what the sequential path gets. `declined_metric` is a frozen dataclass, so sharing the reference is race-free.
- **No namespace-name special case.** The namespace's own name survives as residual like any other token (e.g. `"How many open claims does AnyCompany have?"` leaves `anycompany`). Consuming it would assume the token names the whole dataset rather than filtering it, which the gate cannot verify — it has no schema. Tier 2 does, and receives the token alongside the formula, so the decision is made where the information is. Rejected alternative, recorded so it isn't re-proposed from the diff alone.

The `t1.metric_match` trace step reports `governedDefinitionForwarded` (bool) alongside `unhandledQualifier` and `matchedText`, so whether the definition reached Tier 2 is answerable from the trace without reading Tier-2's prompt.

## Tier 2 retrieval

Tier 2 embeds the query (via Bedrock Cohere Embed v4) and does a k-NN search over the
`semantic_entities_{namespace_short_id}` OpenSearch index to select the relevant
ontology classes/properties for the NL→SQL/SPARQL prompt. The generated SQL/SPARQL
is then translated (Ontop VKG) and executed. Tier 2 produces a result only over
**R2RML-mapped** classes (those carrying a `data_source_id`); a query whose
retrieved classes are all unmapped cannot yield SQL and falls through to Tier 3.

A strategy result does not end Tier 2 when it cannot be an answer: a zero-row
result below confidence 0.3, or Ontop's no-mapping placeholder (`SELECT 1 AS
uselessVariable`, one dummy row at a confident score). Each records a
`t2.sql.failed` / `t2.vkg.failed` step (`reason` `empty_low_confidence` or
`degenerate_no_mapping`) and the chain moves on to the next strategy, then Tier 3.
A pinned strategy (`ontop` or `nl_to_sql`) has no next strategy: the question goes
on to Tier 3. When Tier 3 does not run either (`tierOverride` 2, or 1 with an
explicit `strategy`), a sequential option (`nl_to_sql_first`, the default,
`ontop_first` or `ontop`) returns the placeholder as the Tier 2 answer if no later
strategy answered, as before this check, so a caller pinned to `ontop` still finds
`uselessVariable` in `queryUsed`; from the last strategy it records no failed step.
Under `tierOverride` 1 the placeholder must still clear the 0.3 confidence floor the
orchestrator applies to every Tier 2 answer there. `best` never returns the
placeholder. A question left with no answer gets no result (404).
The Ontop strategy also stops, without a retry, when the VKG translated against
another ontology version than the one Neptune serves (`owl:versionInfo`, read at
most once a minute per namespace and read again before a mismatch is reported,
because the VKG can finish reloading within that minute): while the VKG reloads
after a deploy, or after a failed reload, its `t2.vkg.compile` step reports
`vkg_version_skew` with both versions and no SQL runs. Both sides must name a
`vN`, so a namespace from before versions is unaffected.

A question about a term the namespace has but no table maps is declined rather than
answered with a stand-in. Without this, the writers, which never saw such a term,
answered with whatever looked closest: every client for "the gold loyalty tier" (an
attribute added in the Ontology designer), a `lifetime_value > 10000` threshold for
"premium customers" (a designer subclass), the first order date for a sign-up date the
designer deleted. Both writers are now told the namespace's terms that no table maps
(`tier2/unmapped_terms.py`: one `UnmappedTermsReader` per process, a read kept for 60 s
per namespace and a failed one for 30 s, at most 3 s, at most 50 terms, classes under a
mapped parent and marked properties first): every attribute or relationship marked
`coa:isMapped false`, and every class of the namespace's induced ontologies
(`ontology_type` `induced`, so not a foundational or uploaded one) without
`coa:isMapped true` that is no parent or equivalent of a mapped class, at any depth
(structured queries answer it through the mapped class), and is not named like a mapped
class or like one of their attributes or relationships (a label, a synonym, a table or
column, or a mapped class's IRI local name split into words, ignoring case, separators
and English plurals: a class `Order` found in documents beside the table `orders`, or
`Status` beside the column `orders.status`, is the same thing). NL-to-SQL names their
classes by table, NL-to-SPARQL by label. The rule under the list: a question about one of
these terms, or about anything else the schema does not hold, gets `CANNOT_ANSWER` on the
reply's first line and no query, never a different table, column or filter that stands
in for it; a question the schema does answer is answered as usual. A reply declines when
it starts with `CANNOT_ANSWER`, when its first code block does, or when it has no code
block and says it. A decline (on NL-to-SQL's first shot or its correction, which is told
the same terms; on NL-to-SPARQL before validation, so no validation retry runs) ends that
strategy with a `StrategyDeclined`: its `t2.sql.generate` step is `skipped` with the
detail `cannot_answer` (`{correction_shot, error: cannot_answer}` on a correction), or its
`t2.vkg.translate` step is, and the chain records `t2.sql.failed` / `t2.vkg.failed` with
`reason` `cannot_answer` and moves on to the next strategy, then Tier 3, as for no result
(`best` records it when no strategy answered). Two declines end otherwise: a correction
asked for because a clean first shot returned zero rows keeps that empty result (a
zero-row result is never downgraded to a miss), and a decline on Ontop's VKG retry is a
plain no result that only its `t2.vkg.retry` step records (`reason` `cannot_answer`).
Pinned with `tierOverride` 2, a declined question gets no result (404). A namespace
without class markers lists nothing, and with nothing to list both prompts are
byte-identical to before; a reply is read as a decline only when its prompt listed
terms. A deleted term is not in the graph, so it is not listed; the rule's "anything
else the schema does not hold" is what covers it. The `deep-reasoning` strategy is not
told these terms: its `generate_sql` tool writes SQL as before. TranslateSPARQL uses the
same NL-to-SPARQL writer, so a question it declines gets 400 `QueryTranslationError`
("Could not generate SPARQL for this query").

This is decided in the prompts, not by the unmapped-classes pruning below, for three
reasons: the pruner runs only on the automatic path (a question pinned to Tier 2 or to a
strategy never reaches it), it sees classes only (AOSS stamps provenance on class
documents, not on attributes or relationships, and a deleted term has no document), and
skipping Tier 2 when an unmapped class merely ranks first by some margin would rest on a
similarity threshold rather than on proof, which its fail-open contract rules out.

## Tier pruning (deterministic, fail-open)

Two pruners run on the automatic path (skipped when a `tierOverride` or explicit
`options.strategy` is set) to avoid running a tier that provably cannot contribute.
Both fail **open** — any uncertainty runs the tier.

- **Source-composition gating (per-namespace):** from the namespace's registered
  source types — no DATABASE source → skip Tier 1 + Tier 2; no DOCUMENTS source →
  skip Tier 3's vector sub-step. Emits a `routing.select` trace step with
  `gating: "source_composition"`.
- **Unmapped-classes pruning (per-query, MIXED namespaces only):** for a mixed
  namespace (which per-namespace gating cannot rule out), if the query's top-k
  retrieved ontology classes are **all unmapped** (no `data_source_id`) — and the
  namespace does stamp provenance somewhere (positive-evidence guard) — Tier 2 is
  skipped. Emits `routing.select` with `gating: "unmapped_classes"`. Off via
  `SERVE_TIER2_SKIP_MODE=off`.

## Configuration

| Option | Source | Description |
|--------|--------|-------------|
| `tierOverride` | Request `options` field | Force routing to a specific tier (1, 2, or 3); disables pruning |
| `strategy` | Request `options` field | Pin a Tier-2 strategy (`best`/`ontop`/`nl_to_sql`/`ontop_first`/`nl_to_sql_first` default/`deep-reasoning`); disables per-query pruning. `deep-reasoning` is opt-in only — a Strands tool-use agent, never part of an automatic or fallback chain; `agentic` is the deprecated pre-rename spelling and is still accepted. Distinct from `options.mode="deep-reasoning"`, which replaces the whole tier cascade with the Tier-3 loop. See the strategy table in [../README.md](../README.md#query-routing). |
| `SERVE_TIER2_SKIP_MODE` | Environment variable | `aoss` (default, on for mixed namespaces) or `off` to disable unmapped-classes pruning |
| `TIER3_STRATEGY` | Environment variable | Tier 3 retrieval strategy: `lexical-baseline` (**default**), `hand-rolled`, or `deep-reasoning` (the multi-step reasoning loop as the deployment default). `agentic` is the deprecated spelling of `deep-reasoning` and is still accepted with a warning. |
| `LEXICAL_RETRIEVER_STRATEGY` | Environment variable | Deployment-default graphrag retriever strategy, applied only under `TIER3_STRATEGY=lexical-baseline`: `topic_beam` (**default**), `chunk_based_semantic`, or `traversal`. Invalid values log `invalid_lexical_retriever_strategy` and fall back to `chunk_based_semantic`. |
| `retrieverStrategy` | Request `options` field | Per-request graphrag strategy; engages graphrag on-demand on any deployment (wins over `LEXICAL_RETRIEVER_STRATEGY`). An unknown value is rejected with a 400 `ValidationError`. |

## Tier 3 Retrieval Strategies

Tier 3 supports two retrieval strategies, selected via the `TIER3_STRATEGY` environment variable:

- **`lexical-baseline`** (default): Uses the graphrag-toolkit's built-in retrievers against the lexical knowledge graph that `kg-build` produces — the same graph the unstructured document ingestion pipeline writes to. The deployment-default sub-strategy is `topic_beam` (strongest single-shot on the SEC-10-Q benchmark: 45.13% strict vs 34.36% for hand-rolled, 195 questions). The published ontology graph (`urn:coa:{namespace}:published`) is not consulted — this strategy serves as the baseline against which future ontology-aware retrieval will be measured.

- **`hand-rolled`**: Uses VectorRetriever (OpenSearch k-NN on `chunk_{ns}` index) + GraphTraverser (Neptune SPARQL 1-2 hop traversal). The original implementation, now selectable via `TIER3_STRATEGY=hand-rolled`.

### Selectable graphrag retriever strategy

The graphrag retrieval approach is selectable from a named set (single source of truth: `lexical/strategies.py`):

| Strategy | Engine family | Notes |
|----------|---------------|-------|
| `topic_beam` | `for_semantic_guided_search` | **The `lexical-baseline` deployment default** (`LEXICAL_RETRIEVER_STRATEGY`). Strongest single-shot on the SEC-10-Q benchmark. |
| `chunk_based_semantic` | `for_traversal_based_search` | Fast traversal-based search; the safety fallback when an invalid strategy is configured. |
| `traversal` | `for_traversal_based_search` | The previously-hardcoded weighted set (ChunkBasedSearch@1.0 + EntityNetworkSearch@1.0 + TopicBasedSearch@0.5). |

The lexical retriever is built on every deployment (when the Neptune + OpenSearch endpoints are set), so a request can engage graphrag **on-demand** by passing `options.retrieverStrategy`, regardless of `TIER3_STRATEGY`. The strategy is resolved once per request with precedence **request > deployment default**:

- **Per-request override** — `options.retrieverStrategy`, validated at the model layer so an unknown value returns a **400**. When present it engages graphrag with that strategy on any deployment. Absence is valid.
- **Deployment default** — `LEXICAL_RETRIEVER_STRATEGY` env var (`ServiceConfig.lexical_retriever_strategy`, default `topic_beam`), applied **only when `TIER3_STRATEGY=lexical-baseline`**. An invalid value warns (`invalid_lexical_retriever_strategy`) and falls back to `chunk_based_semantic`. Under `hand-rolled` there is no deployment default, so a request with no `retrieverStrategy` runs the hand-rolled path.

Resolution by deployment:

| `TIER3_STRATEGY` | no `retrieverStrategy` | `retrieverStrategy` passed |
|------------------|------------------------|----------------------------|
| `lexical-baseline` (default) | graphrag with `LEXICAL_RETRIEVER_STRATEGY` (`topic_beam`) | graphrag with that strategy |
| `hand-rolled` | hand-rolled (vector + graph) | graphrag with that strategy |

The resolved strategy name is recorded in the trace / response metadata. When graphrag is engaged with no explicit strategy (the `lexical-baseline` default), it is `topic_beam`.

### Backend-aware store URIs

The lexical retriever derives its graph-store URI from the `NEPTUNE_ENDPOINT` shape: a Neptune Database (NDB) endpoint yields `neptune-db://{host}:8182`, while a Neptune Analytics (NA) endpoint (`g-…`, `neptune-graph://…`, or an NA host) yields `neptune-graph://g-{id}`. The vector store is `aoss://{opensearch_endpoint}`.

### Namespace → tenant contract

`BaselineLexicalRetriever` derives `tenant_id = to_graphrag_tenant_id(namespace)` and passes it to the toolkit factory — the same function `kg-build` uses at ingestion. The derived tenant **must match the value kg-build wrote**, or retrieval targets a non-existent partition and returns zero documents. This namespace→tenant derivation is the production default and is unchanged.

An eval-only `tenant_id_override` constructor seam (sentinel `DEFAULT_TENANT_OVERRIDE`) can force reads under the graphrag-toolkit default tenant; it is used only by the eval/integ harness (which reads a toolkit-built graph) and is **not** wired into `build_lexical_retriever` or the request path — it cannot be reached from an invoke request.
