import { describe, it, expect, beforeEach } from "vitest";
import { useUiStore } from "../../stores/ui-store";

beforeEach(() => {
  useUiStore.setState({ sidebarCollapsed: false });
});

describe("useUiStore", () => {
  it("initializes with sidebar expanded", () => {
    const state = useUiStore.getState();
    expect(state.sidebarCollapsed).toBe(false);
  });

  it("toggleSidebar collapses when expanded", () => {
    useUiStore.getState().toggleSidebar();
    expect(useUiStore.getState().sidebarCollapsed).toBe(true);
  });

  it("toggleSidebar expands when collapsed", () => {
    useUiStore.setState({ sidebarCollapsed: true });
    useUiStore.getState().toggleSidebar();
    expect(useUiStore.getState().sidebarCollapsed).toBe(false);
  });

  it("toggleSidebar toggles multiple times correctly", () => {
    const store = useUiStore.getState();
    store.toggleSidebar();
    expect(useUiStore.getState().sidebarCollapsed).toBe(true);
    useUiStore.getState().toggleSidebar();
    expect(useUiStore.getState().sidebarCollapsed).toBe(false);
    useUiStore.getState().toggleSidebar();
    expect(useUiStore.getState().sidebarCollapsed).toBe(true);
  });

  it("setSidebarCollapsed(true) collapses sidebar", () => {
    useUiStore.getState().setSidebarCollapsed(true);
    expect(useUiStore.getState().sidebarCollapsed).toBe(true);
  });

  it("setSidebarCollapsed(false) expands sidebar", () => {
    useUiStore.setState({ sidebarCollapsed: true });
    useUiStore.getState().setSidebarCollapsed(false);
    expect(useUiStore.getState().sidebarCollapsed).toBe(false);
  });

  it("setSidebarCollapsed with same value is idempotent", () => {
    useUiStore.getState().setSidebarCollapsed(false);
    expect(useUiStore.getState().sidebarCollapsed).toBe(false);
    useUiStore.getState().setSidebarCollapsed(false);
    expect(useUiStore.getState().sidebarCollapsed).toBe(false);
  });
});
