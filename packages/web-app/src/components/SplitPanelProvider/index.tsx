// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * SplitPanelProvider — allows child components to control the AppLayout split panel.
 */
import React, { createContext, useCallback, useContext, useState } from "react";

interface SplitPanelState {
  content: React.ReactNode;
  isOpen: boolean;
  header: string;
}

interface SplitPanelContextValue {
  state: SplitPanelState;
  openPanel: (header: string, content: React.ReactNode) => void;
  closePanel: () => void;
}

const SplitPanelContext = createContext<SplitPanelContextValue>({
  state: { content: null, isOpen: false, header: "" },
  openPanel: () => {},
  closePanel: () => {},
});

export function SplitPanelProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [state, setState] = useState<SplitPanelState>({
    content: null,
    isOpen: false,
    header: "",
  });

  const openPanel = useCallback((header: string, content: React.ReactNode) => {
    setState({ content, isOpen: true, header });
  }, []);

  // Closing a closed panel keeps the state, so its consumers do not render
  // again: a page that closes the panel from an effect cannot loop through them.
  const closePanel = useCallback(() => {
    setState((prev) => (prev.isOpen ? { ...prev, isOpen: false } : prev));
  }, []);

  return (
    <SplitPanelContext.Provider value={{ state, openPanel, closePanel }}>
      {children}
    </SplitPanelContext.Provider>
  );
}

export function useSplitPanel() {
  return useContext(SplitPanelContext);
}
