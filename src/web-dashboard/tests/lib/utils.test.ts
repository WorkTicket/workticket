import { describe, it, expect } from "vitest";
import { cn } from "../../lib/utils";

describe("cn", () => {
  it("returns a string when given string arguments", () => {
    const result = cn("foo", "bar");
    expect(typeof result).toBe("string");
    expect(result).toContain("foo");
    expect(result).toContain("bar");
  });

  it("filters out falsy values", () => {
    const result = cn("foo", false, undefined, null, "", "bar");
    expect(result).toContain("foo");
    expect(result).toContain("bar");
    expect(result).not.toContain("false");
    expect(result).not.toContain("null");
    expect(result).not.toContain("undefined");
  });

  it("handles conditional classes", () => {
    const isActive = true;
    const result = cn("base", isActive && "active");
    expect(result).toContain("base");
    expect(result).toContain("active");
  });

  it("handles conditional false", () => {
    const isActive = false;
    const result = cn("base", isActive && "active");
    expect(result).toContain("base");
    expect(result).not.toContain("active");
  });

  it("handles single argument", () => {
    expect(cn("single")).toContain("single");
  });

  it("handles empty arguments", () => {
    expect(cn()).toBe("");
  });

  it("handles array of classes", () => {
    const result = cn(["foo", "bar"]);
    expect(result).toContain("foo");
    expect(result).toContain("bar");
  });

  it("handles object syntax", () => {
    const result = cn({ foo: true, bar: false, baz: true });
    expect(result).toContain("foo");
    expect(result).toContain("baz");
    expect(result).not.toContain("bar");
  });

  it("deduplicates tailwind classes via twMerge", () => {
    const result = cn("px-4", "px-2");
    expect(result).toContain("px-2");
    expect(result).not.toContain("px-4");
  });
});
