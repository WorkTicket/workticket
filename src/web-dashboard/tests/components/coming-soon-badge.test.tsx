import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ComingSoonBadge } from "../../components/dashboard/coming-soon-badge";

describe("ComingSoonBadge", () => {
  it("renders the badge text", () => {
    render(<ComingSoonBadge />);
    expect(screen.getByText("Coming Soon")).toBeInTheDocument();
  });

  it("renders as a span element", () => {
    const { container } = render(<ComingSoonBadge />);
    const badge = container.firstChild;
    expect(badge?.nodeName).toBe("SPAN");
  });
});
