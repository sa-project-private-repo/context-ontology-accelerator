// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { createContext, useCallback, useContext, useState } from "react";
import type { IconProps } from "@cloudscape-design/components/icon";

export interface DrawerDefinition {
  id: string;
  content: React.ReactNode;
  trigger: {
    iconName?: IconProps.Name;
    iconSvg?: React.ReactNode;
  };
  ariaLabels: {
    drawerName: string;
    closeButton?: string;
    triggerButton?: string;
  };
}

interface ToolsPanelState {
  drawers: DrawerDefinition[];
  activeDrawerId: string | null;
}

interface ToolsPanelContextValue {
  state: ToolsPanelState;
  registerDrawer: (drawer: DrawerDefinition) => void;
  unregisterDrawer: (id: string) => void;
  openDrawer: (id: string) => void;
  closeDrawer: () => void;
  toggleDrawer: (id: string) => void;
}

const ToolsPanelContext = createContext<ToolsPanelContextValue>({
  state: { drawers: [], activeDrawerId: null },
  registerDrawer: () => {},
  unregisterDrawer: () => {},
  openDrawer: () => {},
  closeDrawer: () => {},
  toggleDrawer: () => {},
});

export function ToolsPanelProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [state, setState] = useState<ToolsPanelState>({
    drawers: [],
    activeDrawerId: null,
  });

  const registerDrawer = useCallback((drawer: DrawerDefinition) => {
    setState((prev) => {
      const existing = prev.drawers.findIndex((d) => d.id === drawer.id);
      if (existing >= 0) {
        const updated = [...prev.drawers];
        updated[existing] = drawer;
        return { ...prev, drawers: updated };
      }
      return { ...prev, drawers: [...prev.drawers, drawer] };
    });
  }, []);

  const unregisterDrawer = useCallback((id: string) => {
    setState((prev) => ({
      drawers: prev.drawers.filter((d) => d.id !== id),
      activeDrawerId: prev.activeDrawerId === id ? null : prev.activeDrawerId,
    }));
  }, []);

  const openDrawer = useCallback((id: string) => {
    setState((prev) => ({ ...prev, activeDrawerId: id }));
  }, []);

  // Like the split panel's close: with no drawer open the state stays, and
  // nothing renders again.
  const closeDrawer = useCallback(() => {
    setState((prev) =>
      prev.activeDrawerId === null ? prev : { ...prev, activeDrawerId: null },
    );
  }, []);

  const toggleDrawer = useCallback((id: string) => {
    setState((prev) => ({
      ...prev,
      activeDrawerId: prev.activeDrawerId === id ? null : id,
    }));
  }, []);

  return (
    <ToolsPanelContext.Provider
      value={{
        state,
        registerDrawer,
        unregisterDrawer,
        openDrawer,
        closeDrawer,
        toggleDrawer,
      }}
    >
      {children}
    </ToolsPanelContext.Provider>
  );
}

export function useToolsPanel() {
  return useContext(ToolsPanelContext);
}
