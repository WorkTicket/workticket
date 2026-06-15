import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const mockPost = vi.fn();
const mockGetItem = vi.fn(() => null);
const mockSetItem = vi.fn();
const mockRemoveItem = vi.fn();
const mockSendBeacon = vi.fn();

beforeEach(() => {
  vi.stubGlobal("localStorage", {
    getItem: mockGetItem,
    setItem: mockSetItem,
    removeItem: mockRemoveItem,
  });
  vi.stubGlobal("navigator", {
    sendBeacon: mockSendBeacon,
  });
  vi.stubGlobal("window", {
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    localStorage: {
      getItem: mockGetItem,
      setItem: mockSetItem,
      removeItem: mockRemoveItem,
    },
    location: { pathname: "/dashboard" },
  });
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("analytics event constants", () => {
  it("exports event name constants", async () => {
    const mod = await import("../../lib/analytics");
    expect(mod.PAGE_VIEW).toBeDefined();
    expect(mod.JOB_CREATED).toBeDefined();
    expect(mod.CUSTOMER_CREATED).toBeDefined();
    expect(mod.ESTIMATE_CREATED).toBeDefined();
    expect(typeof mod.PAGE_VIEW).toBe("string");
  });

  it("event names are non-empty strings", async () => {
    const mod = await import("../../lib/analytics");
    const events = [
      mod.PAGE_VIEW,
      mod.JOB_CREATED,
      mod.CUSTOMER_CREATED,
      mod.ESTIMATE_CREATED,
      mod.QUOTE_APPROVED,
      mod.PHOTO_UPLOADED,
      mod.AUDIO_UPLOADED,
      mod.AI_PROCESSING_STARTED,
      mod.AI_PROCESSING_COMPLETED,
      mod.INVOICE_PAID,
      mod.USER_INVITED,
    ];
    events.forEach((e) => {
      expect(typeof e).toBe("string");
      expect((e as string).length).toBeGreaterThan(0);
    });
  });
});

describe("logEvent", () => {
  it("does not throw when called", async () => {
    const { logEvent } = await import("../../lib/analytics");
    expect(() => logEvent("page_view", { page: "/dashboard" })).not.toThrow();
  });

  it("logs without properties", async () => {
    const { logEvent } = await import("../../lib/analytics");
    expect(() => logEvent("test_event")).not.toThrow();
  });
});

describe("analytics module exports", () => {
  it("exports logEvent function", async () => {
    const mod = await import("../../lib/analytics");
    expect(typeof mod.logEvent).toBe("function");
  });

  it("exports flushPendingEvents function", async () => {
    const mod = await import("../../lib/analytics");
    expect(typeof mod.flushPendingEvents).toBe("function");
  });

  it("exports loadQueue function", async () => {
    const mod = await import("../../lib/analytics");
    expect(typeof mod.loadQueue).toBe("function");
  });

  it("exports saveQueue function", async () => {
    const mod = await import("../../lib/analytics");
    expect(typeof mod.saveQueue).toBe("function");
  });
});
