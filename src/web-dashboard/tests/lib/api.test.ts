import { describe, it, expect, vi, beforeEach } from "vitest";

const mockUse = vi.fn();
const mockCreate = vi.fn(() => ({
  interceptors: {
    request: { use: mockUse },
    response: { use: vi.fn() },
  },
}));

vi.mock("axios", () => ({
  default: { create: mockCreate },
}));

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
});

describe("api client", () => {
  it("creates axios instance with correct baseURL", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    mockCreate.mockReturnValue({
      interceptors: {
        request: { use: vi.fn() },
        response: { use: vi.fn() },
      },
    });
    const { api } = await import("../../lib/api");
    expect(mockCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        baseURL: "http://localhost:8000",
        timeout: 15000,
      }),
    );
  });

  it("respects custom NEXT_PUBLIC_API_URL", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    mockCreate.mockReturnValue({
      interceptors: {
        request: { use: vi.fn() },
        response: { use: vi.fn() },
      },
    });
    const { api } = await import("../../lib/api");
    expect(mockCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        baseURL: "https://api.example.com",
      }),
    );
  });

  it("registers request interceptor", async () => {
    const useFn = vi.fn();
    mockCreate.mockReturnValue({
      interceptors: {
        request: { use: useFn },
        response: { use: vi.fn() },
      },
    });
    await import("../../lib/api");
    expect(useFn).toHaveBeenCalled();
  });
});

describe("setTokenGetter", () => {
  it("sets the token getter function", async () => {
    vi.resetModules();
    mockCreate.mockReturnValue({
      interceptors: {
        request: { use: vi.fn() },
        response: { use: vi.fn() },
      },
    });
    const { setTokenGetter } = await import("../../lib/api");
    const mockFn = async () => "test-token";
    expect(() => setTokenGetter(mockFn)).not.toThrow();
  });
});
