# Ontologies

Ontologies are formal knowledge models that describe the concepts, relationships, and rules in your data domain. Context Ontology Accelerator uses ontologies to understand your data semantics — enabling accurate natural language to SQL translation and knowledge graph traversal.

!!! tip "Full request/response schemas"
    For the complete request/response schema for every ontology and induction
    endpoint, see the **[API Reference](#/api-reference)** (Control Plane API
    → Ontology graph + catalog, Ontology induction) — it's generated directly
    from the API contract and always current.

## What is an Ontology?

An ontology defines:

- **Classes**: concepts in your domain (e.g. `Customer`, `Order`, `Product`)
- **Properties**: attributes of classes (e.g. `Customer.email`, `Order.total_amount`)
- **Relationships**: how classes connect (e.g. `Customer places Order`, `Order contains Product`)
- **Constraints**: rules and cardinality (e.g. "every Order must have exactly one Customer")

Context Ontology Accelerator stores ontologies in OWL/Turtle format and materializes them in the Neptune knowledge graph.

## Creating Ontologies

### Automatic Induction

Context Ontology Accelerator can induce an ontology from your approved data sources:

1. Navigate to your namespace → **Ontology** → **Induction**
2. Click **Start induction**
3. Select the data sources to analyze
4. Context Ontology Accelerator examines table structures, column relationships, and metadata to generate an ontology
5. Review and refine the generated model
6. Accept the proposal: it is published as the namespace's next ontology version and, by default, deployed (see "Ontology versions" below)

Induction uses Bedrock (Claude) to analyze table schemas and infer semantic relationships between entities.

#### Grounding

During induction, Context Ontology Accelerator can **ground** new classes against existing ontologies — aligning them to concepts in a loaded foundational ontology (e.g. FIBO) or to your namespace's previously accepted induced ontologies. Grounding emits `rdfs:subClassOf` / `skos:*Match` links so related concepts converge instead of proliferating as duplicates.

The **`groundingMode`** parameter controls how aggressively this runs:

| Mode | Behavior |
|------|----------|
| `NONE` | Grounding is disabled — every class is treated as novel. |
| `STANDARD` | Auto-grounds exact-name matches only; no LLM involved. Fast, conservative. |
| `ENHANCED` *(default)* | Always defers to the LLM reranker to verify domain alignment before grounding — catches semantic matches that names miss, and rejects same-name-different-meaning false matches (e.g. Dublin Core "Policy" ≠ insurance "policy"). |

`ENHANCED` is the default and the right choice for most cases. Use `STANDARD` when you want speed and only trust exact-name alignment, or `NONE` to keep an induced ontology fully independent.

Grounding targets come from `groundingOntologyIds` (foundational/uploaded ontologies you explicitly select) plus the namespace's accepted induced ontologies, which are always grounded against.

In `ENHANCED` mode the LLM reranker returns a small JSON verdict per class or table. Reasoning models spend part of their output budget on internal thinking before that JSON, so on a very long or complex source the answer can be truncated. When that happens the induction job **fails with a clear error** (rather than silently marking everything novel) and the error tells you to raise **`rerankMaxTokens`**. This optional `StartInduction` parameter caps the reranker's output-token budget; it defaults to `1000` (ample for the short verdict on all current models) and accepts `1`–`8192`. You only need to set it in the rare case that error names it — raise it (e.g. to `2000`) and re-run. It has no effect in `STANDARD` or `NONE` mode, which don't call the LLM.

#### Namespace language

Induction writes the text it generates, such as class descriptions,
foreign-key notes and default proposal titles, in the
[namespace language](namespaces.md#namespace-language). Table names, column
names and source-authored metadata are kept as-is.

- **Strategy:** `rigor_ontology` supports only English namespaces. In any other
  namespace, **StartInduction** with that strategy returns `400` before a job
  starts; use `table_to_ontology` instead.
  - **Who this affects:** only namespaces whose language is not English. A
    namespace's language is fixed at creation and every namespace created
    before languages existed is `en`, so existing namespaces, their
    `rigor_ontology` runs and their proposals are unchanged and need no
    re-induction.
  - **Switching:** in a non-English namespace, omit `strategy` or set it to
    `table_to_ontology`, the default; the rest of the request is the same.
    `table_to_ontology` builds classes from table and column names, matches them
    against existing ontologies by embedding similarity and keeps the unmatched
    ones. `rigor_ontology` instead asks an LLM to write and then review a Turtle
    fragment per table, with related existing concepts as context. Its semantic
    naming and grounding are not localized yet, which is why it is limited to
    English namespaces.
  - **Unstructured sources:** document induction (`unstructured_lexical_graph`)
    is not limited by the namespace language.
- **RDF language tags:** in a Japanese (`ja`), Korean (`ko`) or Chinese
  (exactly `zh`) namespace, a generated literal written only in that
  language's scripts carries the namespace tag, e.g. `"制御盤"@ja`. Catalog
  descriptions, synonyms, glossary terms and tags are tagged only when COA
  generated them, and a foreign-key note joined to a column's description only
  when COA generated that description too. A literal that mixes in Latin
  letters, such as `PLC制御盤`, stays untagged, as does every literal in a
  namespace of another language. English namespaces produce the same untagged
  output as before.

#### Overriding grounding choices

A pending proposal shows each class's grounding candidates in its **Grounding**
tab. Select a candidate to replace the automatic choice, or choose **Mark novel
(no grounding)** to keep the class independent. The selection is staged until
you choose **Save changes**.

The API exposes the same workflow through **UpdateProposal**. The modeled
**GetProposal** output names the out-of-band match URL `matchesUrl`, while the
current raw REST JSON still emits `matches_url`. Fetch that presigned URL to
read the grounding candidates. Its JSON uses a `matches` envelope and raw
artifact field names.

```json
{
  "matches": [
    {
      "source_table_identity": "DS#sales::warehouse.public.orders",
      "source_table": "orders",
      "candidates": [
        { "entity_uri": "https://schema.org/Order" }
      ]
    }
  ]
}
```

These nested fields are part of the match artifact, not `ProposalMetadata`.
Use `source_table_identity` as the `groundingOverrides` or `novelTables` key
when it is present. Fall back to `source_table` only when that table name is
unique in the proposal. Use the selected `candidates[].entity_uri` as the
grounding override value. An ambiguous bare table name returns `409 Conflict`
and saves nothing.

```json
{
  "proposalId": "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e",
  "groundingOverrides": {
    "DS#sales::warehouse.public.orders": "https://schema.org/Order"
  },
  "novelTables": ["DS#sales::warehouse.public.customers"]
}
```

To keep a table novel, list it in `novelTables`. `groundingOverrides` values
must be class URIs: a `null` value, which earlier releases accepted as "mark
novel", now returns `422 Unprocessable Entity` and saves nothing. Clients that
sent `"groundingOverrides": {"orders": null}` should send
`"novelTables": ["orders"]` instead, using the same key. A key cannot appear in
both fields.

Grounding choices rebuild the generated ontology and mappings. Save them in a
separate request from `ontologyTurtle` or `r2rmlTurtle` edits; the service
rejects a mixed request and saves neither change.

### Manual Upload

Upload an existing ontology file:

1. Navigate to your namespace → **Ontology** → **Induction**, then use the ontology upload on that page
2. Select your file (formats: Turtle `.ttl`, RDF/XML `.xml`, OWL/XML, JSON-LD)
3. The ontology is ingested into the knowledge graph

### Via the API

Ontologies are managed under `/namespaces/{namespaceId}/ontologies` —
creating an entry, uploading a Turtle/RDF file, and triggering induction (with
optional `groundingOntologyIds` / `groundingMode`, defaulting to `ENHANCED`)
are each separate calls. See **CreateOntology**, **UploadOntology**, and
**StartInduction** in the [API Reference](#/api-reference) for the full
request/response schemas. StartInduction, IngestOntologyFromS3, and
LoadFoundationalOntology are retry-safe with an optional `clientToken` — see
[Retrying Creates Safely](namespaces.md#retrying-creates-safely).

### Saving large proposal edits

The console uses this workflow automatically when a user saves edited proposal
Turtle. Direct API clients can use the same workflow so large `ontologyTurtle`
or `r2rmlTurtle` revisions do not have to fit in the API proxy's 6 MB request
payload.

1. Call **GetProposalUploadUrls** for the pending proposal. Its camelCase
   response contains a `revisionId` plus separate `ontologyPutUrl` and
   `r2rmlPutUrl` targets.
2. Upload each changed Turtle artifact directly to its URL with an HTTP `PUT`,
   `Content-Type: text/turtle`, and `If-None-Match: *`. The precondition is a
   required signed S3 header. The URL carries its own authorization, so do not
   send the control-plane authorization header to S3.
3. After every requested PUT succeeds, call **UpdateProposal** with the same
   `revisionId` as `uploadRevisionId`. Set `ontologyUploaded`, `r2rmlUploaded`,
   or both to `true` to match the artifacts you uploaded.

A shortened ontology-only request follows.

```text
POST /namespaces/<namespaceId>/proposals/<proposalId>/upload-url
{}

200 OK
{
  "revisionId": "d3bc58e9-52a4-43cf-b8b2-44a2f05b43f8",
  "ontologyPutUrl": "https://<signed-s3-url>",
  "r2rmlPutUrl": "https://<signed-s3-url>"
}

PUT <ontologyPutUrl>
Content-Type: text/turtle
If-None-Match: *

<revised ontology Turtle>

POST /namespaces/<namespaceId>/proposals/update
{
  "proposalId": "<proposalId>",
  "ontologyUploaded": true,
  "uploadRevisionId": "d3bc58e9-52a4-43cf-b8b2-44a2f05b43f8"
}
```

Each artifact URL permits one successful object creation. If a PUT returns 409
or 412, or its network outcome is ambiguous, request a fresh revision and
re-upload every changed artifact. Do not reuse the earlier URLs or continue to
**UpdateProposal** with an uncertain write outcome.

**UpdateProposal** reads and validates the created bytes before it points the
proposal at that revision. A missing or invalid artifact leaves the proposal's
current revision unchanged. Keep grounding-choice changes in a separate
**UpdateProposal** request.

## Ontology Lifecycle

| Action | Description |
|--------|-------------|
| **Create** | Register an ontology entry (metadata only) |
| **Induce** | Auto-generate from data source schemas |
| **Upload** | Import from OWL/Turtle/RDF file |
| **Fetch** | Pull from an external URL |
| **Update** | Modify metadata or re-upload |
| **Validate** | Run consistency + quality checks on a proposal before accepting, including datatype checks (see "Validating and repairing datatype issues" below) |
| **Accept** | Publish a proposal as the namespace's next ontology version, deployed right away by default (see "Ontology versions" below) |
| **Deploy / Roll back** | Serve a published version, newer or older, from **Ontology › Induction › Versions** |
| **Delete** | Remove an ontology's graph triples, vector embeddings, and registry entry from the namespace (see "Deleting ontologies" below) |

## Curating an ontology: re-induce and re-accept

Induction reads whatever the catalog says about a table or column at the time
it runs — including any description, synonyms, glossary terms and tags a steward has written (see
[Editing Metadata as a Steward](sources.md#editing-metadata-as-a-steward)). To
get curated text into an accepted ontology, **re-induce the source and accept
the new proposal into the same ontology**. Accepting publishes the namespace's
next ontology version: the new proposal is merged onto the newest published
version, and the namespace serves the result once that version is deployed
(see "Ontology versions" below).

What happens to the class or property you curated, in the new version:

| Surface | On re-accept |
|---------|--------------|
| Description (`rdfs:comment`), synonyms (`skos:altLabel`), glossary terms (`coa:glossaryTerm`), tags (`coa:tag`) | **Replaced.** Only the text in the new proposal is live. The previous text is not deleted — it is kept as history on the vertex (`superseded` on `GET /graph/class`), where you can see what the class used to say. |
| Relationships (`owl:ObjectProperty`, `rdfs:subClassOf`, mappings) | **Kept.** Inferred join paths survive a re-accept; only the text is superseded. |
| Other classes and properties in the same ontology | **Untouched.** Only the classes and properties present in the new proposal are affected. Class identity is the IRI, minted from the table name — so a class whose IRI does not appear in the new proposal is left alone. Two sources that each have a table with the same name resolve to the same IRI; the later accept replaces the earlier text on that class (last writer wins). A renamed table becomes a new class and the old one stays live. A class or property you gave a new IRI in the Ontology designer keeps it: on accept, the re-induced term of its table or column takes that IRI (see "What structured queries answer after an edit" below). |
| Semantic search | The embeddings of the replaced text are retired with it, so search stops returning the old phrasing. |

This means a curated description **wins** over the AI-generated one it
replaces: the model that answers questions sees one definition, not two.

!!! tip "Starting from a clean synonym list"
    The metadata editor pre-fills the synonyms that are already stored, which
    for an enriched source are the AI's suggestions. If you only *add* terms,
    the suggestions are saved too. Use **Clear all** in the synonyms field to
    start from an empty, authored list — saving replaces the whole list.

!!! warning "Curate before you approve the source"
    Today a metadata edit made *after* the source is approved is saved and
    shown, but does not reach the next induction. Edit descriptions and
    synonyms while the source is still in review, then approve, then induce.
    Propagating post-approval edits is tracked separately.

## Ontology versions

Accepting a proposal does not write into the served ontology directly. It
**publishes** the namespace's next **ontology version**: an immutable snapshot
of the ontology, its mappings and the files the text-to-SQL service (VKG)
loads, merged onto the newest published version. What the namespace serves
(the knowledge graph, the search embeddings and the VKG files) changes only
when a version is **deployed**.

1. **Accept** a reviewed proposal. With **Deploy immediately** checked (the
   default) the new version is deployed as soon as it is published; uncheck it
   to look at the version before serving it.
2. **Deploy** from **Ontology › Induction › Versions**. Each row
   shows who published the version, when, and a summary of the class and
   property changes; **Details** shows the full diff and links to download the
   snapshot files. **Deploy** serves a newer version, **Roll back** an older
   one, and **Redeploy** runs the active version again (or finishes a
   deployment that failed part-way). The tab follows a running deployment step
   by step. Its last step, **Request VKG reload**, asks the VKG to load the
   version's mappings; the VKG keeps serving the previous mappings until it
   has, which takes a few minutes after the deployment reads `Deployed`.

A proposal that reads `accepted` has been **published**, not yet served: the
knowledge graph, the search embeddings and the ontology list (`ListOntologies`)
change when the deployment the accept started finishes, usually about 30
seconds later, and the VKG follows within a few minutes. A client that reads
served state after an accept takes the version from the proposal's
`publishedVersion` and waits until `ListOntologyVersions` reports
`latestDeployment` for that version with status `DEPLOYED` (or polls
`GetOntologyDeployment` with the proposal's `autoDeployId`). When
`autoDeployHeld` is set instead, no deployment started.

A namespace runs one deployment at a time: another deployment, or a delete of
an ontology, is refused while one runs.

The next version always builds on the newest published version, not on the
active one. After a rollback, or while a newer version is published but not
deployed, the Versions tab and the Ontology designer say **The next version
builds on v*N*, not the active v*M*; its changes carry over unless you revert
them.** A change you rolled back from returns with the next accept unless you
undo it in the draft.

!!! warning "Governed metrics a version would break"
    When governed metrics are linked to classes the version removes, the deploy
    dialog lists them and stays disabled until you tick **I understand these
    metrics will break**. An automatic deploy after accept is held instead, and
    the proposal says why; deploy that version from Ontology › Induction › Versions once you
    have reviewed the impact.

**Retired versions.** Deleting the namespace's induced ontology stops serving
it: nothing stays deployed, and the namespace's VKG service is scaled to zero.
Its versions stay in the list as history, marked **Retired**, and can no longer
be deployed (the API answers `409 Conflict`). The next accepted proposal starts
a new version history without a parent, and deploying it serves the namespace
again.

## Editing a proposal in the Ontology designer

The **Ontology designer** edits a proposal as the ontology it would become. The
draft is the namespace's newest version with the proposal merged onto it,
exactly what accepting the proposal would publish, and you edit it as a graph.
Open it with **Open in Ontology designer** on the proposal page; the proposal
must still be in review (pending, updated, or after a failed accept). Once the
draft is open, the same button reads **Continue in Ontology designer**.

### Editing

- **Right-click a class** in the graph to add a subclass, add a relationship to
  another class, rename it or delete it. Deleting asks whether the properties
  that use the class are kept (it is removed from their domain or range, and
  their table mappings go with the class) or deleted with it. Its subclasses
  move up to its parents, and its table mapping and the joins into it are
  removed so the mapping stays valid. A kept attribute or relationship that is
  mapped from another class's table, and whose domain or range was moved up to
  the deleted class, gets that mapped class back as its domain or range, with a
  warning.
- **Click a class** to edit it in the **Inspector**: label, description, IRI,
  parents, attributes, grounding to a reference ontology, and, for a property,
  its domain and range. The Inspector also shows the class's table mappings and
  a few instances from the domain data. **Mappings** lists a class's own maps
  when it has them. A class with no map of its own reads "Structured queries can
  use this term only through a mapped class below it or equivalent to it." when
  structured queries still answer it that way, and "Structured queries cannot
  use this term." when they do not. A parent, a domain or a range is a link
  only when the draft declares it. One that comes from a reference ontology, an
  XSD datatype range or an anonymous class is shown as plain text, because there
  is nothing to open.
- **Shift-click** (or Ctrl/Cmd-click) several classes to delete them together
  (**Delete selected**) or, for two, to merge one into the other (**Merge…**).
  References move to the class you keep. The other class's labels become
  alternative labels, its parents, restrictions and other axioms move to the
  class you keep, and its descriptions move for the languages the class you keep
  has none in. Only classes shown in the graph are deleted.
- New classes and properties get IRIs made from their labels the way induction
  makes them; labels in any script are kept readable. A label needs at least
  one letter or digit.
- **Undo** and **Redo** step through your changes; **History** lists every
  revision with who made it and what it did, newest first (**Load older
  revisions** reads the next 50).
- **Click a class** also keeps its attributes (the small red circles) around it
  on the graph after the pointer leaves it, until you click elsewhere.

Every change is checked before it is applied: a parent, domain or range must be
a class of the draft, a subclass cycle is refused, and a change that cannot
apply changes nothing. A label shared with another term of the same kind is
allowed but reported.

A property mapped to a table keeps what its mapping gives it, so a change that
contradicts the mapping is refused with the reason: its domain must stay the
class of its table or one of that class's parents, an attribute's datatype must
stay its column's, and a relationship's range must stay the class it joins to
or one of that class's parents. Moving the domain or range to one of those
parents is allowed with a warning: SPARQL through the VKG then offers the
attribute on its new domain and the join on its new range only, not on their
subclasses, while NL-to-SQL keeps reading them from the table the mapping
names.
While a mapped property's domain or range relies on such a parent, removing
that parent from the class, or a grounding change that removes it, is refused.
The Inspector does not offer **Change datatype** for a mapped attribute and
says which column decides it. A change that replaces a datatype everywhere in
the draft, as the assistant may propose, is refused too when a mapped column
has that datatype, or when a mapped attribute would get another datatype than
its column's; the error names each such attribute with its column. Repairing a
malformed datatype name (see "Datatype issues" below) still works. A
relationship mapped through an IRI template can
change its range, with a warning, because its target class is not known.
Merging two classes mapped from different tables is allowed with a warning:
structured queries then describe them as one table. Tables with the same name
in two schemas or two datasources are different tables.

If someone else changed the draft first, your change is not applied and the
page says **Another change was applied first** and reloads the draft; check it
and apply your change again.

While a proposal is open in the Ontology designer, its Turtle, entity and
grounding editors on the proposal page are read-only. Validate and Cancel still
work on the proposal page, and Validate checks the draft. Once the proposal is
accepted, rejected or cancelled, its draft stays readable but no change is
accepted any more.

A role that reads the namespace but cannot change its ontology (`data-analyst`,
`platform-viewer`) can continue to an open draft, read it, its checks, history
and questions, and query it. The console turns off, and says why, what the API
would refuse that role: editing, undo and redo, messages to the assistant,
applying or discarding a change set, full validation, saving, deleting or
checking a question, rebasing, accepting and opening a draft; on the proposal
page, editing, validating, inferring constraints, cancelling and accepting the
proposal; starting an induction; deploying on the **Versions** tab; rebuilding
the domain data and changing its mappings; loading, uploading and deleting a
reference ontology; deleting an induced ontology. The API still decides
whatever the console offers.

### Asking the assistant

The **Chat** tab talks with an assistant about the draft. Ask it to review a
part of the ontology or to make a change, for example "Medication needs a parent
class Treatment". The classes selected in the graph go with your message, so
"this class" means them. The assistant reads the draft (classes, properties,
mappings and the instances of the domain data) and answers. Expand **Steps** to
see what it looked at.

The assistant never changes the draft itself. It **proposes change sets**,
shown as cards in the chat with the reason and one line per change:

- **Show on graph** draws the draft as the change set would leave it: added
  classes and edges in green, changed ones in orange and removed ones in red
  (dashed). **Hide preview** goes back to the draft.
- **Apply all** applies the change set as one revision. Untick changes to
  preview and apply only the others (**Apply selected**). If a change you keep
  needs one you left out, for example a relationship to a class the same change
  set adds, the card says which change does not apply and nothing changes.
- **Discard** drops it.

An applied change set is a revision like any other: **History** marks it
**Assistant** with its reason, and **Undo** takes it back. If the draft changed
after the assistant proposed a change set, applying it checks it again; when it
no longer fits, the card says **The draft changed** and nothing is applied.

One message is answered at a time per proposal. The assistant answers with what
it has after 25 lookups or 5 minutes. It remembers the last few exchanges and a
summary of earlier ones. A read-only draft (accepted, rejected or cancelled)
takes no messages.

### Checking the draft

The **Checks** tab checks the revision on screen, again after every change:

- **Findings** list structural problems, errors first: a domain or range that is
  not a class of the draft, a subclass cycle, a class used as an attribute's
  datatype, a missing or duplicate label, and classes with no relationship,
  parent or attribute. Click a term to open it in the Inspector, or **Ask the
  assistant** to have the problems explained and fixes proposed.
- **Structured queries** lists what the structured query path (SQL over the
  mapped tables) cannot answer once the draft is deployed: classes with no
  table mapping (a class is left out when structured queries answer it through
  a mapped class: when a mapped class reaches it by going up `rdfs:subClassOf`
  and across `owl:equivalentClass`, at any depth, or when the class itself
  states `owl:equivalentClass` to a mapped
  class), and the attributes and relationships of a mapped class (or
  relationships into one) that have no column or join behind them. It also
  lists classes mapped from more than one table, which structured queries treat
  as one table, and mapped terms whose label is not their table or column name:
  structured queries keep using the table or column name. A draft that maps no
  table any more, although the version it was built on did, is a warning:
  structured queries are off after deploy. So are mapped classes, attributes and
  relationships of that version that the draft deletes: structured queries no
  longer answer from those classes' tables, or use those columns and joins. Each
  deleted class is shown with its tables and each deleted attribute with its
  column. A class you rename or merge, or an attribute or relationship you
  rename, keeps its table mapping and is not listed.
  Click a term to open it in the Inspector; a deleted one is shown as text.
- **Compared with the data** says which mapped tables the source catalog lacks,
  which classes with instances in the domain data the draft removes or renames,
  how many document mappings would go stale, and which governed metrics would
  break. Each part is checked on its own; one that cannot be checked says so.
- **Run full validation** runs the proposal's validation (the same as
  **Validate** on the proposal page) on the draft and lists its findings.

### Querying

The **Query** tab runs read-only queries, at most 1,000 rows within 20 seconds:

- **Draft + documents (SPARQL)** reads the draft's classes and properties
  together with the instances extracted from documents. Write the query without
  `FROM`, `FROM NAMED`, `GRAPH` or `SERVICE`, query hints or `\u` escapes; the
  console chooses the graphs and the timeout. A `LIMIT` above 1,000 is refused.
- **Lexical graph (Cypher)** reads the documents' entities, facts and chunks.
  Every node pattern needs one of the namespace's labels, such as
  `` `__Entity__<tenant>__` ``; **Insert sample** writes one for you. After a
  `WITH`, label the nodes again. `UNION`, `CALL` and write clauses are refused.
- **Database via VKG (SPARQL)** runs `SELECT` or `ASK` on the source databases
  through the namespace's virtual knowledge graph, with your own table grants.
  It follows the deployed version's mappings, not the draft's. Right after a
  deploy, until the VKG has loaded that version, a query is refused with a message
  that names the version the VKG still serves; run it again shortly. If the
  refusal does not go away, the VKG did not load the deployed version. A query
  whose answers need an IRI the deployed ontology does not have, a class no table
  maps, or an attribute or relationship added to a mapped class in the designer
  is refused at once with a message that names each such IRI. Only the terms
  every answer needs count: one inside `OPTIONAL`, `MINUS`, a `FILTER` or a
  subquery, or in only one branch of a `UNION`, leaves the query to the VKG. Any
  other query that no table mapping matches, such as one on an attribute of a
  class added in the designer, or an attribute used on a class whose table does
  not have it, is refused with "No table mapping matches this query, so the VKG
  cannot answer it." A parent class of a mapped class can be queried: the VKG
  answers it with the rows of its mapped subclasses.

IRIs in the results open the term in the Inspector. A query the assistant ran
shows in the chat with **Open in Query console**.

### Competency questions

The **Questions** tab keeps the questions the ontology must answer, for example
"Which medications did each patient receive?". Every draft of the namespace
shares them. **Check** asks the assistant to test a question against this draft:
it writes a SPARQL query, runs it, and records its verdict on the question,
which you follow in the Chat tab:

- **Answerable**: the query returns rows.
- **Expressible, no data**: the draft can express the question, but no data
  answers it yet. A verdict of answerable whose query returns nothing is stored
  as this.
- **Not expressible**: classes or relationships the question needs are missing;
  the assistant may propose a change set that adds them.

**Open query** opens the query of the latest check in the Query tab. Changing a
question's text clears its verdict.

### Domain data in the graph

When the namespace has domain data, each class shows how many instances the
active version's domain data has. A class only the draft has shows **new**: its
instances appear once the version is deployed. Counts are dimmed, with the
reason, while the domain data does not match the active version.

### Large ontologies

Above 150 classes the graph shows one class and its neighbours up to two steps
away, starting with the most connected class. Use **Find a class**, or
double-click a class, to move around. For an ontology of 150 classes or fewer,
**Show all classes** goes back to the whole graph.

The graph keeps class names readable: as you zoom out, classes are drawn
larger, up to twice their size. A graph too large to show whole at that size
opens around the selected class or the class you focused on, else around its
most connected class or its middle; the minimap shows the rest, and the
fit-view control shows the whole graph.

### Rebasing

If another version is published after you opened the draft, Accept is disabled
and the page shows **A newer version was published** with a **Rebase** button:
the proposal is merged onto the newest version again and your changes are
replayed on it. The Accept dialog can find this out too: if it learns of a newer
version that the page has not seen yet, it says **Rebase first** and the same
banner appears behind it. A change that no longer applies (for example, a class
the new version already has) is listed in **Some changes do not apply to the
newest version**. A change that refers to a term created by a change you skip,
or by one that does not apply, is listed with it. They all start ticked, and
**Rebase without the checked ops** leaves them out and replays the rest. Right
after a rebase, one Undo takes back all replayed changes at once.

### Datatype issues

A malformed datatype name, such as `xsd:datetime` instead of `xsd:dateTime`, is
found in the draft just as in any proposal: by **Validate** on the proposal page
and by the check that runs when you accept from that page. **Repair** rewrites
the name in the draft, in its ontology and in its mappings, as one new revision.
It appears in **History**, and Undo takes it back. **Repair & accept** repairs
the draft and then accepts the revision the repair wrote. If someone changed the
draft in the meantime, nothing is repaired and the draft reloads. A repair that
would give a mapped attribute another datatype than its column's is refused with
the column's name and datatype, and nothing is repaired. A column typed with
another malformed name for the same datatype, such as `xsd:timestamp` for
`xsd:dateTime`, counts as that datatype and is repaired too. As a
property's datatype, the Ontology designer accepts the common XSD datatypes
(strings, numbers, dates and times, binary, URIs and names), spelled exactly:
`xsd:dateTime`, not `xsd:datetime`. It also accepts `rdfs:Literal`,
`rdf:langString` and `rdf:PlainLiteral`. `xsd:ID`, `xsd:IDREF`, `xsd:IDREFS`,
`xsd:ENTITY`, `xsd:ENTITIES`, `xsd:NMTOKENS`, `xsd:NOTATION` and the abstract
`xsd:anyType`, `xsd:anySimpleType` and `xsd:anyAtomicType` are not accepted.
Accepting from the Ontology designer does not run this check.

### Accepting

**Accept** shows what the next version changes compared with the version the
draft was built on: removed classes and properties, renamed or merged classes,
and governed metrics that link to removed classes (an automatic deploy is held
until you deploy from the Versions tab and acknowledge them). It also lists the
added or changed terms that structured queries cannot answer (**Not answered by
structured queries**), with any class an edit to another term leaves unanswered
(a parent that a mapped class no longer sits below), and the mapped classes, attributes and relationships the
draft deletes, with their tables or column (**Removed from structured queries**).
The next version is the draft exactly as shown, so
deleted and renamed terms are gone from it,
and the version's diff lists them. Accepting a proposal that was never opened in
the Ontology designer still merges it onto the newest version as before.

If a newer version exists when you accept from the proposal page, or is
published while the accept is running, the proposal page says **Rebase
required** with a link to the Ontology designer: rebase the draft there, review
it, and accept again.

!!! note "Limits in this release"
    The Ontology designer does not create new table mappings; database mappings
    come from induction. A class, attribute or relationship added in the draft
    therefore has no table mapping, and structured queries cannot answer it
    until its datasource is re-induced; the Checks tab and the Accept dialog
    list them. A class added as a parent of a mapped class, or as an equivalent
    of one, is the exception: structured queries answer it through that mapped
    class, and it is not listed. A class that itself states
    `owl:equivalentClass` to a class that is only answered that way is listed,
    because that hop reaches only a mapped class. A class or property renamed
    in the draft loses the compiled constraints (SHACL shapes) of its old IRI.

### What structured queries answer after an edit

Structured queries (Tier 2: SPARQL through the VKG, and NL-to-SQL) read the
tables the version's mappings name. Once the accepted draft is deployed and the
VKG has loaded it:

- **A class you added**, a subclass included, has no table mapping, so
  structured queries do not offer it; facts extracted from documents can fill
  it after deploy. A class a mapped class reaches, by going up
  `rdfs:subClassOf` and across `owl:equivalentClass` at any depth, is answered
  through that mapped class instead, and so is a class that itself states
  `owl:equivalentClass` to a mapped class; neither is listed. The hop the other
  way round reaches only a mapped class: a class that states
  `owl:equivalentClass` to a class that is itself only answered that way is
  listed.
- **An attribute or relationship you added** to a mapped class, or a
  relationship you added from another class into a mapped class, has no column
  or join behind it. The deploy marks it unmapped, and structured queries leave
  it out (see "Mapped classes and Tier-2 answerability" below).
- **A question about one of these terms**, or about a term you deleted, is not
  answered from the closest table or column (every client for "the gold loyalty
  tier", say). Structured queries are told which terms no table maps and decline
  such a question, so it goes on to the documents (Tier 3). Whether a question is
  about such a term is the model's call.
- **A new label** on a mapped class or attribute does not rename its table or
  column. The deploy records the table and column names from the mappings, and
  NL-to-SQL keeps writing those names; the new label goes along as a synonym of
  the table or a hint on the column. A class mapped from more than one table is
  still described by its labels.
- **A new IRI** for a mapped class or property replaces the old one in its
  mappings too, so structured queries keep answering it. When its table is induced again
  later, accepting that proposal (or opening it in the Ontology designer) keeps
  the IRI you chose and maps the re-induced table to it, so the inducer's IRI
  does not come back beside it. This does not hold for a class that is also
  mapped from a table the new induction leaves out, such as one you merged with
  another table's class: the re-induced table brings its own class back.
- **Deleting a mapped class** removes its table mapping and the joins into it.
- **A domain or range moved up to a parent** of its mapped class (allowed, with
  a warning) moves the property in SPARQL through the VKG, which then offers it
  on the parent. NL-to-SQL keeps the column or join on the table the mapping
  reads: the deploy records which class that is. Deleting that parent later
  puts the property back on its mapped class.
- **An edit that contradicts a mapping** is refused when you make it (see
  "Editing" above).

### Via the API

See **OpenStudioDraft**, **GetStudioDraft**, **GetStudioGraph**,
**GetStudioTerm**, **SearchStudioTerms**, **ApplyStudioChanges**,
**UndoStudioChange**, **RedoStudioChange**, **ListStudioHistory**,
**RebaseStudioDraft** and **GetStudioAcceptPreview** in the
[API Reference](#/api-reference), and for the assistant **SendStudioMessage**,
**ListStudioEvents**, **ListStudioChangeSets**, **PreviewStudioChangeSet**,
**ApplyStudioChangeSet** and **DiscardStudioChangeSet**, and for checks and
queries **GetStudioChecks**, **RunStudioQuery**, **ListStudioQuestions**,
**PutStudioQuestion**, **DeleteStudioQuestion**, **CheckStudioQuestion** and the
data layer's **RunSparqlQuery**. Reading, checking and running a query need
`viewNamespace` (**RunSparqlQuery** needs `query`); opening, every change, sending
a message, applying or discarding a change set, and saving, deleting or checking
a question need `manageOntology`. Every change names the revision it was made
against (`expectedRevision`) and answers with the new one; accepting a proposal
open in the Ontology designer, and repairing its datatypes, also need
`expectedRevision`. A proposal that is no longer in review refuses every change
(409 `ProposalNotEditable`).

## Domain data

The namespace's ingested documents become **domain data** (the *domain ABox*):
typed **instances** of the ontology's classes, the **relations** between them,
their **attributes**, and the **evidence** behind each one. The domain data
always conforms to the **active ontology version**: an instance has a class of
that version, and a relation is kept only when its property's domain and range
admit both ends. Database rows are not copied; tables stay virtual and are
queried through the VKG.

A build starts from the lexical graph the document pipeline extracted at
ingestion, whose class and relation names are free text. It **maps** those
names onto the ontology's classes and properties (see "Mappings" below). For a
class the mapping leaves without instances, it reads the document chunks
nearest to the class description and asks the model for instances of it; a
name that does not appear in the chunk is dropped. The same inputs always give
the same domain data, and the same name in the same class is always the same
instance.

Tier 3 answers use the domain data once it conforms to the deployed version
(see [Domain facts](serve.md#domain-facts)).

### When the domain data is rebuilt

The domain data is rebuilt from scratch, in the background, after:

- a **Deploy**, **Roll back** or **Redeploy**, including one that failed after
  it may already have switched the served version
- a document source finishing **ingestion** or a **re-scan**
- a document source being **deleted**: instances and evidence found only in
  that source disappear
- a mapping change: **Change target**, **Ignore**, **Use free attribute** or
  **Reset to auto**
- **Rebuild** on the **Domain data** tab

A namespace runs one build at a time; requests that arrive during a build are
merged into one more build. A failed build keeps the previous domain data and
shows the error. Neither a build nor its failure affects a deployment or a
query. **Deleting the induced ontology clears the domain data**; the mapping
table is kept.

### The Domain data tab

**Induction** page, **Domain data** tab:

- **Domain ABox** card. The build status (**Ready**, **Building**, **Rebuild
  queued**, **Build failed** or **Not built**), the version line (e.g.
  `Built for v2, active v3`), when it was last built, and the number of
  **Instances**, **Relations**, **Attributes** and **Evidence**. **Out of
  date** means the data was built for a version that is no longer active;
  answers do not use it until the rebuild lands. **Last build failed** shows the
  error. **Rebuild** queues a build (disabled when no version is active), and
  the tab refreshes on its own while a build runs or waits.
- **Instances**. Filter by class or find by label (a case-insensitive
  substring), then **Load more** to page. Open an instance to see its
  **Attributes** (a **free** badge marks a free attribute), its **Links** in
  both directions (each opens the linked instance), and its **Evidence**. A busy
  instance shows its first 200 incoming links and first 50 evidence chunks.
- **Mappings**. One table per kind (**Classes**, **Relations**,
  **Attributes**), filterable by status, with the columns **Found in
  documents**, **Maps to**, **Origin**, **Status** and **Rationale**.

### Mappings

Each mapping row ties a name found in the documents to an ontology term:

| Kind | Found in documents | Maps to |
|------|--------------------|---------|
| **Classes** | An extracted class name, e.g. `Company` | An ontology class, or **Ignored** |
| **Relations** | A relation between two mapped classes, e.g. `Customer -PLACED-> Order` | An object property, forward or inverse, or **Ignored** |
| **Attributes** | An attribute of a mapped class, e.g. `Customer: email` | A datatype property, or **Free attribute** (kept under its own name) |

- **Decided automatically.** Each name is compared with the ontology's terms by
  embedding similarity, and the model picks the matching candidate or none,
  with a one-line **Rationale**. A row is decided again only when it is new or
  its candidate terms changed; the others are reused. These rows have origin
  `auto`.
- **Editable.** **Change target** opens a search for a class, an object
  property or a datatype property, by the row's kind; pick one and **Save** (a
  relation also takes a **Direction**). **Ignore** (classes and relations) and **Use free
  attribute** (attributes) are in the same dialog. Every change queues a
  rebuild.
- **Manual overrides are preserved.** An edited row has origin `manual`, and
  automatic decisions never overwrite it, across deploys and rollbacks.
  **Reset to auto** hands the row back to the automatic decision, made again at
  the next build.
- **Stale rows.** A row whose target is not a term of the active version (for
  example a manual choice after a deploy or rollback removed that term) is
  **Stale** and is not applied. It applies again once a deployed version has
  the term; **Reset to auto** lets the automatic decision choose again.
- **Ignored rows.** A class or relation mapped to **Ignored** yields no
  instances or relations. When no ontology term fits, the automatic decision
  ignores a class or relation and keeps an attribute as a free attribute.

The target search lists terms from every ontology in the namespace. A term
that is not in the active version is refused (`422`), and the dialog shows the
error.

### Where evidence comes from

Evidence is always a **chunk of an ingested document**: the chunk the document
pipeline read a fact from, or the chunk the model read during re-extraction.
Each instance, relation and attribute keeps up to five evidence chunks, each
with its document source and file. The tab titles each chunk with the file name
and expands it to the chunk text (up to 1,000 characters). Only the current
version of a document counts; chunks of a superseded version are not evidence.

### Via the API

See **GetDomainAbox**, **ListDomainInstances**, **GetDomainInstance**,
**ListAboxMappings**, **UpdateAboxMapping**, **ResetAboxMapping** and
**RebuildDomainAbox** in the [API Reference](#/api-reference). Reading needs
`viewNamespace`; editing a mapping and rebuilding need `manageOntology`.

## Re-scanning and re-induction

Connected database sources can be **re-scanned** to pick up schema changes, and a re-scan goes through steward review before it is approved. An induction reflects the source schema **at the time it was induced**, so once a source is re-scanned and re-approved, an earlier induction can fall out of date.

The namespace **Ontology** page shows a warning naming any approved database source whose newest induction predates its latest approved re-scan. There are two cases:

- **The accepted ontology is stale**: the loaded ontology was induced before the re-scan. Accepting a re-induction publishes a new version **merged** onto the previous one (append-only): it does not remove tables dropped at the source. Descriptions and synonyms are replaced (see "Curating an ontology: re-induce and re-accept" above), but a changed label (`rdfs:label`) is added next to the old one. For a clean rebuild, **delete the affected induction and induce again**. Editing an induction in place is not available yet.
- **A pending induction predates the re-scan** — a proposal is awaiting review, but the source was re-scanned after that induction was triggered, so the proposal was built on the older schema. **Re-run the induction on the current source, then accept the new proposal** rather than the out-of-date one.

The warning clears once the source's newest induction is newer than its newest approved re-scan — that is, after you re-induce (it clears even while the new proposal is still pending review). A source that was never induced is never flagged.

## Induction Job Status and Troubleshooting

Induction runs **asynchronously**. When you trigger it (via **Start induction** or the **StartInduction** operation), Context Ontology Accelerator returns a job that moves through several in-progress stages before reaching one of two terminal states:

| Status | Meaning |
|--------|---------|
| `pending` | Queued, not yet started. |
| *in-progress* (`fetching_metadata`, `matching`, `building_ontology`, `storing`, …) | The job is running through its stages. |
| `completed` | Success — a proposal is ready to review and accept. |
| `failed` | No proposal was produced; check the job's `error` field. |

### Why a job fails

- **Grounding can't reach OpenSearch (the vector search backend).** The most common cause: the grounding step hits a transport error against OpenSearch — throttling (`429`), a `5xx`, or a connection/timeout — that persists after automatic retries. Transient OpenSearch errors are retried automatically (6 attempts with increasing backoff), so most blips never surface as a failure.
- **Data or configuration problems** — for example, no tables were found in the selected data sources.

### Fail-loud reliability guarantee

A failed job means the ontology was **not** silently degraded. If grounding cannot complete, the system refuses to hand back an ontology with missing grounding — which would look like a legitimately all-novel schema, with no signal that anything went wrong — and fails the job instead. A `failed` status is a clear, honest signal, not a misleading result.

### Checking status and re-running

Poll the job returned by **StartInduction** to see its current status; on failure, the `error` field names the failure type, so you can tell a transient outage from a configuration problem. See **StartInduction** in the [API Reference](#/api-reference) for the request/response schema.

- **Failed on a transient OpenSearch error?** Just re-run the induction — the error is often gone on retry.
- **Failed on a data or configuration problem?** Fix the source selection or configuration first (e.g. make sure the selected sources actually contain tables), then re-run.

## Deleting ontologies

Deleting an ontology removes its named graph triples from Neptune, its vector
embeddings from the search index, and its registry entry. The delete runs
asynchronously: the ontology stays listed as **Delete in progress** until the
graph and embeddings are actually clean, then the row disappears. Deleting an
ontology also removes the proposals tied to it (those that fed into it and those
grounded against it). Deleting the induced ontology also stops serving it,
retires its versions and clears the domain data (see "Ontology versions" and
"Domain data" above). If a delete fails, the
row says so and **Delete** stays available, so you can retry it.

**Reversibility depends on the ontology type:**

- **Foundational** (curated references such as FIBO or Schema.org, loaded from
  the catalog): removal is **reversible** — you can re-load the same ontology
  from the catalog afterward if you need it again.
- **Uploaded** (your own OWL/Turtle/RDF file) and **induced** (auto-generated
  from your data): removal is **permanent** — this is your own content and
  cannot be recovered. Re-creating it means uploading the file again or
  re-running induction.

**Dependency guard.** While any **induced** ontology exists in the namespace,
you cannot delete a **non-induced** (foundational or uploaded) ontology — the
induced ontology is derived from and grounded against those references, so
removing one out from under it would leave it dangling. The API rejects this
with `409 Conflict`, and the UI disables the **Delete** action on those rows
(with an explanation) rather than letting you click into an error. Deleting an
**induced** ontology itself is always allowed (it excludes itself from the
check). To resolve a blocked delete: first delete the induced ontology (or
ontologies) that depend on the reference, then delete the reference.

**Where each Delete lives in the web app:**

- **Foundational / uploaded** — **Induction** page → **Reference ontologies**
  tab → the row's **Delete** action.
- **Induced** — **Explorer** → **Ontologies** tab → open the ontology → **Delete
  ontology**. (Induced ontologies aren't listed on the Induction page, so this is
  the path to use when the guard above tells you to remove one first.)

## Validating and repairing datatype issues

Before you accept a proposal, **Validate** runs consistency and quality checks on
it. One of those checks looks at the **datatype names** on your properties.

Some datatype tokens are *malformed* — a mis-cased XSD type (`xsd:datetime` with a
lowercase "t" instead of the correct `xsd:dateTime`) or a SQL-style alias that
isn't a real XSD type (`xsd:varchar`, which should be `xsd:string`). These usually
come from hand-editing a proposal's Turtle or uploading an ontology authored
elsewhere — induction itself always emits valid types.

**Editing a proposal keeps your Turtle exactly as you wrote it.** The system no
longer silently rewrites datatype tokens when you save — your ontology is your
data. Instead, when malformed tokens are present, Validate surfaces them as a
`MALFORMED_DATATYPE_TOKENS` finding (a non-blocking warning) that lists each one
and the correctly-spelled type it would become.

**Repairing is a one-click, opt-in action.** The finding shows a **Repair** button;
clicking it previews the exact changes (e.g. `datetime → dateTime`, and how many
properties are affected) and, on confirm, rewrites the tokens in your saved
ontology. Nothing changes until you consent.

**Repair is recommended but optional.** Your queries work either way — when a
proposal is accepted, the copy served to the query engine is automatically
normalized, so malformed tokens never break querying. Repairing matters for
*consistency*: it keeps your **saved** ontology (and anything you download from it)
spelled the same as what actually gets served. If you accept a proposal that still
has unrepaired tokens, you'll get a confirmation prompt first — you can repair then,
or accept as-is.

## Mapped classes and Tier-2 answerability

Not every class in an ontology can answer a **structured** query. Only classes
backed by a real SQL table can — Context Ontology Accelerator marks these as **mapped**.

- **What makes a class "mapped"?** A class is mapped when it is the target of an
  R2RML mapping (the `rr:class` of a TriplesMap) — i.e. it corresponds to a real
  table/view in a data source and is therefore SQL-queryable. Classes from
  **document (unstructured) induction** or from a **loaded foundational ontology**
  (e.g. FIBO, Schema.org) have no table behind them and are **unmapped**.
- **Why it matters:** the structured-query path (Tier 2 — NL→SPARQL via the VKG,
  and NL→SQL) is filtered to **mapped** classes only. This stops the system from
  authoring a query against a class that has no data behind it (which would
  return nothing). Unmapped classes still power Tier-3 knowledge retrieval and
  graph traversal — they're only excluded from structured queries.
- **Attributes and relationships need a mapping too.** An attribute or
  relationship (an `owl:DatatypeProperty` or `owl:ObjectProperty`) of a mapped
  class that no R2RML mapping maps (for example one added in the Ontology
  designer, with no column or join behind it) is marked unmapped when its version
  is deployed. So is a relationship from another class into a
  mapped class that no mapping maps, because the NL-to-SQL foreign-key walk
  follows a relationship from either end. Structured queries leave them out:
  neither query writer is offered them, and a generated SPARQL query that names
  one goes back to the writer as naming an unknown term. A namespace deployed
  before properties were marked keeps every property.
- **A question about an unmapped term goes to the documents.** Leaving a term out
  is not enough: asked about it, a writer that had never seen it answered from
  the closest table or column. So both writers are also told the terms no table
  maps (unmapped attributes and relationships, and the unmapped classes of induced
  ontologies other than a parent of a mapped class or one named like a mapped
  class or column), and decline a question about one, which then goes on to
  Tier 3. See the `strategy` notes in [Serve](serve.md).
- **Tables and columns keep their names.** The deploy also records the table a
  mapped class is read from and the column a mapped attribute is read from
  (`coa:physicalName`, when the class reads one table and the attribute one
  column). The NL-to-SQL class documents and the foreign-key walk name tables
  and columns by it, so a label edited in the Ontology designer never renames
  what the SQL writer uses; the label goes along as a synonym or a hint. When
  the domain or range of a mapped attribute or relationship is not the class its
  mapping reads it from or joins it to (it was moved up to a parent), the deploy
  records that class (`coa:mappedDomain`, `coa:mappedRange`), and NL-to-SQL
  places the column or join there.
- **How to tell:** `GET /graph/class` returns an **`is_mapped`** boolean per
  class. `true` means R2RML-backed and eligible for Tier 2; `false`/absent means
  it is not.
- **Trust boundary:** `is_mapped` is **server-derived** from the R2RML mapping
  when a version is deployed (and at ingest). It cannot be set by uploading an
  ontology that asserts the marker itself: any caller-supplied `coa:isMapped` or
  `coa:physicalName` triple is stripped from an uploaded ontology and from a
  proposal before it is published, so the flag always reflects a genuine SQL
  mapping.

## How Ontologies Power Queries

Once an ontology is ingested:

1. **Tier 2 (structured queries — NL-to-SPARQL / NL-to-SQL)**: The ontology provides semantic context for translating natural language into SPARQL/SQL. The VKG (Virtual Knowledge Graph) uses the ontology to map between the conceptual model and the physical database schema. **Only mapped classes** (see above) are exposed to this path.

2. **Tier 3 (Knowledge Retrieval)**: The Neptune knowledge graph stores ontology entities as nodes. Semantic search and graph traversal use these to find relevant context for answers.

3. **Embeddings**: Ontology classes and properties are embedded in OpenSearch for vector similarity search, enabling the system to match natural language queries to the right concepts.

## Viewing Ontologies

The web app separates *starting* ontology work from *browsing* what already exists:

- The **Induction** page is where you start an induction run, review and accept
  proposals, and manage the reference ontologies (foundational + uploaded) used
  for grounding.
- The **Explorer** page (reachable from Induction via **Open Explorer**) is where
  you browse what exists in the namespace. It has three tabs:
  - **Classes** — the class hierarchy, relationships, and properties, with an
    interactive graph view of the knowledge graph.
  - **Ontologies** — an inventory of every ontology in the namespace (induced,
    foundational, and uploaded), filterable by type.
  - **Inducted sources** — the data sources that have fed an induction run.

The Classes tab's graph view seeds with every class in the namespace on first
open (across all its ontologies) **plus each ontology's object-property
relationships**, so the initial picture matches the graph shown on an accepted
proposal's detail page rather than a class-only skeleton. A relationship is
drawn as an edge when the property declares an `rdfs:domain` and `rdfs:range`
that are both classes in the view — which is what the induction engine emits
for foreign-key and inferred relationships. (Some imported foundational
vocabularies describe their properties differently, so they may show as a
class hierarchy with few or no relationship edges; their classes still render
in full.)

Very large ontologies are bounded so layout and rendering stay responsive, and
the limit applies **per ontology** rather than to the namespace total:

- An ontology with **500 classes or fewer** renders in full, with its
  relationships.
- One above that is reduced to a bounded set (its largest-subtree root classes
  plus a capped number of descendants).
- Across the whole view, at most **1500** classes and **1000** relationship
  edges are drawn up front; ontologies are filled in induced-first, then
  smallest-first.

Because the limit is per ontology, loading a large reference ontology for
grounding (Schema.org, FIBO) reduces only *that* ontology — your induced
ontology keeps every class and relationship beside it. Anything not drawn up
front is still reachable through search and by selecting a node, which upgrades
it to the full class detail including all its relationships and attributes.

## Best Practices

- **Start with induction**: let Context Ontology Accelerator generate a baseline ontology from your sources, then refine
- **One ontology per domain**: keep ontologies focused on a single business domain. Spread ontologies across logically separated namespaces as needed.
