// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// @examples for com.amazon.semanticcontext.graphstudio operations.
// Extracted from the service definition to keep operation/shape
// definitions readable. `apply` merges the @examples trait back onto
// each operation, so the doc-coverage gate in linters.smithy still holds.
$version: "2"

namespace com.amazon.semanticcontext.graphstudio

apply OpenStudioDraft @examples([
    {
        title: "Open a proposal in the Ontology designer"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110" }
        output: {
            proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110"
            status: "pending"
            editable: true
            revision: 0
            baseVersion: 3
            headVersion: 3
            rebaseRequired: false
            ontologyId: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#"
            counts: { classes: 42, objectProperties: 18, datatypeProperties: 120, triplesMaps: 40 }
            canUndo: false
            canRedo: false
            openedAt: "2026-10-19T09:00:00+00:00"
            openedBy: "steward@example.com"
            updatedAt: "2026-10-19T09:00:00+00:00"
            updatedBy: "steward@example.com"
        }
    }
])

apply GetStudioDraft @examples([
    {
        title: "Read a draft that needs a rebase"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110" }
        output: {
            proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110"
            status: "updated"
            editable: true
            revision: 7
            baseVersion: 3
            headVersion: 4
            rebaseRequired: true
            ontologyId: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#"
            counts: { classes: 41, objectProperties: 18, datatypeProperties: 118, triplesMaps: 39 }
            canUndo: true
            canRedo: false
            openedAt: "2026-10-19T09:00:00+00:00"
            openedBy: "steward@example.com"
            updatedAt: "2026-10-19T09:40:00+00:00"
            updatedBy: "steward@example.com"
        }
    }
])

apply GetStudioGraph @examples([
    {
        title: "Read the graph with ABox badges"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110" }
        output: {
            revision: 7
            mode: "full"
            totalClasses: 2
            truncated: false
            nodes: [
                {
                    iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Customer"
                    label: "Customer"
                    grounded: true
                    editable: true
                    attributes: [
                        {
                            iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#customer_email"
                            label: "email"
                            range: "http://www.w3.org/2001/XMLSchema#string"
                        }
                    ]
                    attributeCount: 1
                    aboxCount: 74
                    afterDeploy: false
                }
                {
                    iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Shipment"
                    label: "Shipment"
                    grounded: false
                    editable: true
                    attributes: []
                    attributeCount: 0
                    afterDeploy: true
                }
            ]
            edges: []
            abox: { status: "READY", current: true, conformsToVersion: 3, activeVersion: 3 }
        }
    }
])

apply GetStudioTerm @examples([
    {
        title: "Read a class for the Inspector"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Customer" }
        output: {
            iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Customer"
            kind: "class"
            label: "Customer"
            labels: [
                {
                    value: "Customer"
                }
            ]
            altLabels: []
            comment: "A person or company that buys"
            editable: true
            parents: [
                {
                    iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Party"
                    label: "Party"
                    declared: true
                }
                {
                    iri: "https://spec.edmcouncil.org/fibo/ontology/FND/AgentsAndPeople/Agents/Agent"
                    label: "Agent"
                    declared: false
                }
            ]
            children: []
            matches: [
                {
                    target: "https://spec.edmcouncil.org/fibo/ontology/FND/AgentsAndPeople/Agents/Agent"
                    kind: "exact"
                }
            ]
            properties: [
                {
                    iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#customer_email"
                    label: "email"
                    kind: "datatypeProperty"
                    direction: "outgoing"
                    other: "http://www.w3.org/2001/XMLSchema#string"
                    otherLabel: "string"
                    otherDeclared: false
                }
            ]
            mappings: [
                {
                    triplesMap: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#TriplesMap_Customer"
                    table: "customer"
                }
            ]
            abox: { aboxCount: 74, afterDeploy: false }
        }
    }
])

apply SearchStudioTerms @examples([
    {
        title: "Find classes by label"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", q: "cust", kind: "class", limit: 10 }
        output: {
            revision: 7
            terms: [
                {
                    iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Customer"
                    label: "Customer"
                    kind: "class"
                }
            ]
        }
    }
])

apply ApplyStudioChanges @examples([
    {
        title: "Delete a class and the properties that use it"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110"
            ops: [
                {
                    op: "delete_class"
                    iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Legacy"
                    cascade: "delete"
                }
            ]
            expectedRevision: 7
        }
        output: {
            revision: 8
            dryRun: false
            warnings: []
            created: []
            diff: {
                classes: {
                    added: []
                    removed: ["http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Legacy"]
                    changed: []
                }
                objectProperties: {
                    added: []
                    removed: []
                    changed: []
                }
                datatypeProperties: {
                    added: []
                    removed: []
                    changed: []
                }
            }
            canUndo: true
            canRedo: false
        }
    }
    {
        title: "Repair a malformed datatype token that came with the proposal"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110"
            ops: [
                {
                    op: "replace_datatype"
                    from: "http://www.w3.org/2001/XMLSchema#datetime"
                    to: "http://www.w3.org/2001/XMLSchema#dateTime"
                }
            ]
            expectedRevision: 7
            dryRun: true
        }
        output: {
            revision: 7
            dryRun: true
            warnings: []
            created: []
            diff: {
                classes: {
                    added: []
                    removed: []
                    changed: []
                }
                objectProperties: {
                    added: []
                    removed: []
                    changed: []
                }
                datatypeProperties: {
                    added: []
                    removed: []
                    changed: ["http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#order_date"]
                }
            }
            canUndo: true
            canRedo: false
        }
    }
])

apply UndoStudioChange @examples([
    {
        title: "Undo the last change set"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", expectedRevision: 8 }
        output: {
            revision: 9
            dryRun: false
            warnings: []
            created: []
            diff: {
                classes: {
                    added: ["http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Legacy"]
                    removed: []
                    changed: []
                }
                objectProperties: {
                    added: []
                    removed: []
                    changed: []
                }
                datatypeProperties: {
                    added: []
                    removed: []
                    changed: []
                }
            }
            canUndo: false
            canRedo: true
        }
    }
])

apply RedoStudioChange @examples([
    {
        title: "Redo the undone change set"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", expectedRevision: 9 }
        output: {
            revision: 10
            dryRun: false
            warnings: []
            created: []
            diff: {
                classes: {
                    added: []
                    removed: ["http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Legacy"]
                    changed: []
                }
                objectProperties: {
                    added: []
                    removed: []
                    changed: []
                }
                datatypeProperties: {
                    added: []
                    removed: []
                    changed: []
                }
            }
            canUndo: true
            canRedo: false
        }
    }
])

apply ListStudioHistory @examples([
    {
        title: "List the newest revisions"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", maxResults: 2 }
        output: {
            revision: 8
            entries: [
                {
                    revision: 8
                    kind: "apply"
                    source: "agent"
                    rationale: "Legacy duplicates Customer and no table maps to it."
                    changeSetId: "20261020T094100000000Z-3f9b0add"
                    author: "steward@example.com"
                    at: "2026-10-19T09:41:00+00:00"
                    summary: ["Deleted class Legacy (delete its properties)"]
                    summaryMessages: [
                        {
                            code: "DELETE_CLASS_DELETE"
                            params: { term: "Legacy" }
                        }
                    ]
                    opCount: 1
                    warnings: []
                    active: true
                    undone: false
                }
                {
                    revision: 7
                    kind: "apply"
                    source: "user"
                    author: "steward@example.com"
                    at: "2026-10-19T09:40:00+00:00"
                    summary: ["Added class Shipment"]
                    summaryMessages: [
                        {
                            code: "ADD_CLASS"
                            params: { label: "Shipment" }
                        }
                    ]
                    opCount: 1
                    warnings: []
                    active: true
                    undone: false
                }
            ]
            canUndo: true
            canRedo: false
            nextToken: "eyJQSyI6IjBmMWUyZDNjLTRiNWEtNDk2OC04Nzc2LTY1NTQ0MzMyMjExMCIsIlNLIjoiNiIsInNjb3BlIjoic3R1ZGlvLWhpc3Rvcnk6YTFiMmMzZDQtZTVmNi00YTdiLThjOWQtMGUxZjJhM2I0YzVkOjBmMWUyZDNjLTRiNWEtNDk2OC04Nzc2LTY1NTQ0MzMyMjExMCJ9"
        }
    }
])

apply RebaseStudioDraft @examples([
    {
        title: "Rebase onto the newest version"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110"
            expectedRevision: 8
            skipOps: []
        }
        output: {
            revision: 9
            baseVersion: 4
            replayed: 2
            skipped: []
            canUndo: true
            canRedo: false
        }
    }
])

apply GetStudioAcceptPreview @examples([
    {
        title: "Preview what accepting the draft publishes"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110" }
        output: {
            revision: 8
            baseVersion: 3
            headVersion: 3
            rebaseRequired: false
            diff: {
                classes: {
                    added: []
                    removed: ["http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Legacy"]
                    changed: []
                }
                objectProperties: {
                    added: []
                    removed: []
                    changed: []
                }
                datatypeProperties: {
                    added: []
                    removed: []
                    changed: []
                }
            }
            renamed: []
            breakingMetrics: [
                {
                    metricId: "legacy_revenue"
                    name: "Legacy revenue"
                    missingClassIri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Legacy"
                }
            ]
            structured: {
                unmappedClasses: {
                    count: 0
                    terms: []
                }
                unmappedProperties: {
                    count: 0
                    terms: []
                }
                labelDiffers: {
                    count: 0
                    terms: []
                }
                multiTableClasses: {
                    count: 0
                    terms: []
                }
                removedMappedClasses: {
                    count: 0
                    terms: []
                }
                removedMappedProperties: {
                    count: 0
                    terms: []
                }
                mapsNoTable: false
            }
        }
    }
])

apply SendStudioMessage @examples([
    {
        title: "Ask the assistant about the selected class"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110"
            text: "Medication needs a parent class Treatment"
            selection: ["http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medication"]
        }
        output: { turnId: "20261020T093958000000Z-1c2d3e4f", seq: 12 }
    }
])

apply ListStudioEvents @examples([
    {
        title: "Poll the events of a running turn"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", after: 12 }
        output: {
            events: [
                {
                    seq: 13
                    turnId: "20261020T093958000000Z-1c2d3e4f"
                    type: "tool_call"
                    at: "2026-10-20T09:40:00+00:00"
                    tool: "find_terms"
                    input: "query=\"treatment\""
                }
                {
                    seq: 14
                    turnId: "20261020T093958000000Z-1c2d3e4f"
                    type: "tool_result"
                    at: "2026-10-20T09:40:01+00:00"
                    tool: "find_terms"
                    status: "success"
                    summary: "1 term for 'treatment'"
                }
                {
                    seq: 15
                    turnId: "20261020T093958000000Z-1c2d3e4f"
                    type: "change_set"
                    at: "2026-10-20T09:40:03+00:00"
                    changeSetId: "20261020T094000000000Z-7eea78bc"
                    opSummaries: ["Made Medication a subclass of Treatment"]
                }
            ]
            lastSeq: 15
            more: false
            turn: { turnId: "20261020T093958000000Z-1c2d3e4f", status: "running", author: "steward@example.com", startedAt: "2026-10-20T09:39:58+00:00" }
        }
    }
])

apply ListStudioChangeSets @examples([
    {
        title: "List the pending change sets"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", status: "pending" }
        output: {
            revision: 9
            changeSets: [
                {
                    changeSetId: "20261020T094000000000Z-7eea78bc"
                    turnId: "20261020T093958000000Z-1c2d3e4f"
                    status: "pending"
                    createdAt: "2026-10-20T09:40:03+00:00"
                    createdRevision: 9
                    createdBy: "steward@example.com"
                    rationale: "Medication is a kind of treatment in the clinical guidelines."
                    ops: [
                        {
                            op: "add_parent"
                            iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medication"
                            parent: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Treatment"
                        }
                    ]
                    summary: ["Made Medication a subclass of Treatment"]
                    warnings: []
                    diffCounts: {
                        classes: { added: 0, removed: 0, changed: 1 }
                        objectProperties: { added: 0, removed: 0, changed: 0 }
                        datatypeProperties: { added: 0, removed: 0, changed: 0 }
                    }
                }
            ]
        }
    }
])

apply PreviewStudioChangeSet @examples([
    {
        title: "Preview a change set that adds a parent"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", changeSetId: "20261020T094000000000Z-7eea78bc" }
        output: {
            revision: 9
            changeSetId: "20261020T094000000000Z-7eea78bc"
            opIndexes: [0]
            applies: true
            warnings: []
            created: []
            diff: {
                classes: {
                    added: []
                    removed: []
                    changed: ["http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medication"]
                }
                objectProperties: {
                    added: []
                    removed: []
                    changed: []
                }
                datatypeProperties: {
                    added: []
                    removed: []
                    changed: []
                }
            }
            graph: {
                revision: 9
                mode: "full"
                totalClasses: 2
                truncated: false
                nodes: [
                    {
                        iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medication"
                        label: "Medication"
                        grounded: false
                        editable: true
                        attributes: []
                        attributeCount: 0
                        afterDeploy: false
                        preview: "changed"
                    }
                    {
                        iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Treatment"
                        label: "Treatment"
                        grounded: false
                        editable: true
                        attributes: []
                        attributeCount: 0
                        afterDeploy: false
                    }
                ]
                edges: [
                    {
                        id: "sub|http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medication|http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Treatment"
                        kind: "subClassOf"
                        source: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medication"
                        target: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Treatment"
                        preview: "added"
                    }
                ]
            }
        }
    }
    {
        title: "Preview only an op that depends on one left out"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", changeSetId: "20261020T094000000000Z-7eea78bc", opIndexes: "1" }
        output: {
            revision: 9
            changeSetId: "20261020T094000000000Z-7eea78bc"
            opIndexes: [1]
            applies: false
            error: { message: "domain http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Supplier is not a class of the draft", opIndex: 1 }
            warnings: []
            created: []
        }
    }
])

apply ApplyStudioChangeSet @examples([
    {
        title: "Apply all ops of a change set"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", changeSetId: "20261020T094000000000Z-7eea78bc", expectedRevision: 9 }
        output: {
            revision: 10
            dryRun: false
            warnings: []
            created: []
            diff: {
                classes: {
                    added: []
                    removed: []
                    changed: ["http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medication"]
                }
                objectProperties: {
                    added: []
                    removed: []
                    changed: []
                }
                datatypeProperties: {
                    added: []
                    removed: []
                    changed: []
                }
            }
            canUndo: true
            canRedo: false
            changeSet: {
                changeSetId: "20261020T094000000000Z-7eea78bc"
                turnId: "20261020T093958000000Z-1c2d3e4f"
                status: "applied"
                createdAt: "2026-10-20T09:40:03+00:00"
                createdRevision: 9
                createdBy: "steward@example.com"
                rationale: "Medication is a kind of treatment in the clinical guidelines."
                ops: [
                    {
                        op: "add_parent"
                        iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medication"
                        parent: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Treatment"
                    }
                ]
                summary: ["Made Medication a subclass of Treatment"]
                warnings: []
                appliedRevision: 10
                appliedOpIndexes: [0]
                appliedBy: "steward@example.com"
                appliedAt: "2026-10-20T09:41:00+00:00"
            }
        }
    }
])

apply DiscardStudioChangeSet @examples([
    {
        title: "Discard a change set"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", changeSetId: "20261020T094000000000Z-7eea78bc" }
        output: {
            changeSetId: "20261020T094000000000Z-7eea78bc"
            turnId: "20261020T093958000000Z-1c2d3e4f"
            status: "discarded"
            createdAt: "2026-10-20T09:40:03+00:00"
            createdRevision: 9
            createdBy: "steward@example.com"
            rationale: "Medication is a kind of treatment in the clinical guidelines."
            ops: [
                {
                    op: "add_parent"
                    iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medication"
                    parent: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Treatment"
                }
            ]
            summary: ["Made Medication a subclass of Treatment"]
            warnings: []
            discardedBy: "steward@example.com"
            discardedAt: "2026-10-20T09:42:00+00:00"
        }
    }
])

apply GetStudioChecks @examples([
    {
        title: "Check a draft"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110" }
        output: {
            revision: 10
            instant: {
                findings: [
                    {
                        code: "DANGLING_RANGE"
                        severity: "error"
                        message: "The range of prescribed by is Physician, which is not a class of the draft"
                        messageCode: "DANGLING_RANGE"
                        messageParams: { property: "prescribed by", range: "Physician" }
                        iris: ["http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Physician", "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#medication_prescribedBy"]
                    }
                    {
                        code: "ISOLATED_CLASS"
                        severity: "info"
                        message: "Ward has no relationship, parent or attribute"
                        messageCode: "ISOLATED_CLASS"
                        messageParams: { term: "Ward" }
                        iris: ["http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Ward"]
                    }
                ]
                counts: { error: 1, warning: 0, info: 1 }
                truncated: false
            }
            structured: {
                unmappedClasses: {
                    count: 1
                    terms: [
                        {
                            iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Ward"
                            label: "Ward"
                        }
                    ]
                }
                unmappedProperties: {
                    count: 1
                    terms: [
                        {
                            iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#medications_dosageNote"
                            label: "dosage note"
                            domainIri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medications"
                        }
                    ]
                }
                labelDiffers: {
                    count: 1
                    terms: [
                        {
                            iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medications"
                            label: "Medication"
                            physicalName: "medications"
                        }
                    ]
                }
                multiTableClasses: {
                    count: 0
                    terms: []
                }
                removedMappedClasses: {
                    count: 0
                    terms: []
                }
                removedMappedProperties: {
                    count: 1
                    terms: [
                        {
                            iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#medications_legacyCode"
                            label: "legacy_code"
                            domainIri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medications"
                            physicalName: "legacy_code"
                        }
                    ]
                }
                mapsNoTable: false
            }
            data: {
                tables: {
                    checked: true
                    missing: []
                }
                abox: {
                    available: true
                    current: true
                    impacts: [
                        {
                            classIri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Doctor"
                            instances: 34
                            change: "renamed"
                            toIri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Physician"
                        }
                    ]
                }
                mappings: {
                    available: true
                    stale: 0
                    staleTargets: []
                }
                metrics: {
                    breaking: []
                }
            }
        }
    }
])

apply RunStudioQuery @examples([
    {
        title: "List the classes of a draft"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", language: "sparql", target: "draft", query: "PREFIX owl: <http://www.w3.org/2002/07/owl#> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> SELECT ?c ?label WHERE { ?c a owl:Class ; rdfs:label ?label } ORDER BY ?label LIMIT 2" }
        output: {
            language: "sparql"
            target: "draft"
            revision: 10
            form: "SELECT"
            columns: ["c", "label"]
            rows: [
                [
                    {
                        type: "uri"
                        value: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Medication"
                    }
                    {
                        type: "literal"
                        value: "Medication"
                    }
                ]
                [
                    {
                        type: "uri"
                        value: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#Patient"
                    }
                    {
                        type: "literal"
                        value: "Patient"
                    }
                ]
            ]
            rowCount: 2
            truncated: false
            elapsedMs: 182
        }
    }
    {
        title: "A query that names its own graph is refused"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", language: "sparql", target: "draft", query: "SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }" }
        error: {
            shapeId: "com.amazon.semanticcontext.common#ValidationError"
            content: { message: "GRAPH is not allowed: the console chooses the graphs a query reads" }
        }
        allowConstraintErrors: true
    }
])

apply ListStudioQuestions @examples([
    {
        title: "List the competency questions"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d" }
        output: {
            questions: [
                {
                    questionId: "6f1c2d3e-q1"
                    text: "Which medications did each patient receive?"
                    expectation: "Patient names with medication names"
                    lastCheck: {
                        proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110"
                        revision: 10
                        status: "expressible"
                        query: "SELECT ?p ?m WHERE { ?p <http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/induced#patient_receives> ?m }"
                        rowCount: 0
                        sample: []
                        explanation: "The draft relates patients to medications, but no document mentions one yet."
                        checkedAt: "2026-10-21T09:10:00+00:00"
                    }
                    createdAt: "2026-10-21T09:00:00+00:00"
                    updatedAt: "2026-10-21T09:00:00+00:00"
                    updatedBy: "steward@example.com"
                }
            ]
        }
    }
])

apply PutStudioQuestion @examples([
    {
        title: "Add a competency question"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", questionId: "6f1c2d3e-q1", text: "Which medications did each patient receive?", expectation: "Patient names with medication names" }
        output: { questionId: "6f1c2d3e-q1", text: "Which medications did each patient receive?", expectation: "Patient names with medication names", createdAt: "2026-10-21T09:00:00+00:00", updatedAt: "2026-10-21T09:00:00+00:00", updatedBy: "steward@example.com" }
    }
])

apply DeleteStudioQuestion @examples([
    {
        title: "Delete a competency question"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", questionId: "6f1c2d3e-q1" }
        output: {}
    }
])

apply CheckStudioQuestion @examples([
    {
        title: "Ask the assistant to check a question"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "0f1e2d3c-4b5a-4968-8776-655443322110", questionId: "6f1c2d3e-q1" }
        output: { turnId: "20261021T091000000000Z-5a6b7c8d", seq: 41 }
    }
])
