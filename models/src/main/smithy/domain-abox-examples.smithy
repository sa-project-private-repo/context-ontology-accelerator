// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// @examples for com.amazon.semanticcontext.domainabox operations.
// Extracted from the service definition to keep operation/shape
// definitions readable. `apply` merges the @examples trait back onto
// each operation, so the doc-coverage gate in linters.smithy still holds.
$version: "2"

namespace com.amazon.semanticcontext.domainabox

apply GetDomainAbox @examples([
    {
        title: "Read the state of a built domain ABox"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d" }
        output: {
            status: "READY"
            current: true
            conformsToVersion: 2
            activeVersion: 2
            buildId: "20261015T101500Z-5e6f7a8b"
            builtAt: "2026-10-15T10:17:42+00:00"
            counts: { instances: 128, relations: 214, attributes: 305, evidence: 612 }
            classCounts: [
                {
                    classIri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/class/Customer"
                    label: "Customer"
                    count: 74
                }
                {
                    classIri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/class/Order"
                    label: "Order"
                    count: 54
                }
            ]
            mappingRevision: "9d3b1c7e5a2f4086"
            pending: false
        }
    }
])

apply ListDomainInstances @examples([
    {
        title: "Search the instances of a class by label"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", classIri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/class/Customer", q: "acme", maxResults: 50 }
        output: {
            instances: [
                {
                    iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/data/Customer/3f9a1c5e7b2d4a60"
                    classIri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/class/Customer"
                    classLabel: "Customer"
                    label: "Acme Corporation"
                    altLabels: ["ACME Corp", "Acme"]
                    evidenceCount: 7
                }
            ]
        }
    }
])

apply GetDomainInstance @examples([
    {
        title: "Get one instance with its relations and evidence"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/data/Customer/3f9a1c5e7b2d4a60" }
        output: {
            iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/data/Customer/3f9a1c5e7b2d4a60"
            classes: [
                {
                    iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/class/Customer"
                    label: "Customer"
                }
            ]
            label: "Acme Corporation"
            altLabels: ["ACME Corp", "Acme"]
            attributes: [
                {
                    property: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/Customer/DatatypeProperty/industry"
                    label: "industry"
                    value: "Manufacturing"
                    free: false
                    evidence: ["chunk-0042"]
                }
            ]
            outgoing: [
                {
                    property: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/Customer/ObjectProperty/placed_order"
                    label: "placed order"
                    target: { iri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/data/Order/b81d6f02a94c35e7", label: "Order 1042", classIri: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/class/Order" }
                    evidence: ["chunk-0042", "chunk-0057"]
                }
            ]
            incoming: []
            evidence: [
                {
                    chunkId: "chunk-0042"
                    docSourceId: "01J9ZQ8XN4T7VBR6C2K9YHMED0"
                    sourceKey: "documents/contracts/acme-services-agreement.pdf"
                    text: "Acme Corporation, a manufacturing customer, placed order 1042 on 2026-03-02."
                }
            ]
            truncated: { incoming: false, evidence: false }
        }
    }
])

apply ListAboxMappings @examples([
    {
        title: "List the active class mappings"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", kind: "class", status: "active" }
        output: {
            mappings: [
                {
                    id: "class.0a1b2c3d4e5f60718293a4b5c6d7e8f9"
                    kind: "class"
                    key: ["Client"]
                    target: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/class/Customer"
                    targetLabel: "Customer"
                    direction: "forward"
                    origin: "auto"
                    status: "active"
                    rationale: "Client is a synonym of Customer."
                    decidedForVersion: 2
                    updatedBy: "system"
                    updatedAt: "2026-10-15T10:16:03+00:00"
                }
            ]
        }
    }
])

apply UpdateAboxMapping @examples([
    {
        title: "Map a lexical class to another ontology class by hand"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", mappingId: "class.0a1b2c3d4e5f60718293a4b5c6d7e8f9", target: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/class/Account" }
        output: {
            id: "class.0a1b2c3d4e5f60718293a4b5c6d7e8f9"
            kind: "class"
            key: ["Client"]
            target: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/class/Account"
            targetLabel: "Account"
            direction: "forward"
            origin: "manual"
            status: "active"
            rationale: "Client is a synonym of Customer."
            decidedForVersion: 2
            updatedBy: "alice@example.com"
            updatedAt: "2026-10-16T08:30:00+00:00"
        }
    }
])

apply ResetAboxMapping @examples([
    {
        title: "Return a manual mapping to automatic decision (its target stays until the rebuild)"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", mappingId: "class.0a1b2c3d4e5f60718293a4b5c6d7e8f9" }
        output: {
            id: "class.0a1b2c3d4e5f60718293a4b5c6d7e8f9"
            kind: "class"
            key: ["Client"]
            target: "http://coa.amazon.com/namespace/a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d/ontology/sales/class/Account"
            targetLabel: "Account"
            direction: "forward"
            origin: "auto"
            status: "active"
            rationale: ""
            decidedForVersion: 2
            updatedBy: "alice@example.com"
            updatedAt: "2026-10-16T08:31:12+00:00"
        }
    }
])

apply RebuildDomainAbox @examples([
    {
        title: "Queue a rebuild"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d" }
        output: { requested: true }
    }
])
