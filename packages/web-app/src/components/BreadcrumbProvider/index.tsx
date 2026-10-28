// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * BreadcrumbProvider — lets a page publish dynamic breadcrumb items into the
 * AppLayout `breadcrumbs` slot (the bar below the top nav, next to the side-nav
 * toggle). Pages that don't set any render no breadcrumb.
 *
 * Mirrors the SplitPanelProvider pattern: shell reads `items` and renders a
 * Cloudscape BreadcrumbGroup; pages call `setBreadcrumbs`/`clearBreadcrumbs`.
 */
import React, { createContext, useCallback, useContext, useState } from "react";

/** A single breadcrumb crumb. `onClick` runs instead of navigation when set. */
export interface BreadcrumbItem {
  text: string;
  /** Optional handler; when present the crumb acts as an in-page action. */
  onClick?: () => void;
}

interface BreadcrumbContextValue {
  items: BreadcrumbItem[];
  setBreadcrumbs: (items: BreadcrumbItem[]) => void;
  clearBreadcrumbs: () => void;
}

const BreadcrumbContext = createContext<BreadcrumbContextValue>({
  items: [],
  setBreadcrumbs: () => {},
  clearBreadcrumbs: () => {},
});

export function BreadcrumbProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [items, setItems] = useState<BreadcrumbItem[]>([]);

  const setBreadcrumbs = useCallback((next: BreadcrumbItem[]) => {
    setItems(next);
  }, []);

  // Clearing an empty trail keeps it, so the shell does not render again.
  const clearBreadcrumbs = useCallback(() => {
    setItems((prev) => (prev.length === 0 ? prev : []));
  }, []);

  return (
    <BreadcrumbContext.Provider
      value={{ items, setBreadcrumbs, clearBreadcrumbs }}
    >
      {children}
    </BreadcrumbContext.Provider>
  );
}

export function useBreadcrumbs() {
  return useContext(BreadcrumbContext);
}
