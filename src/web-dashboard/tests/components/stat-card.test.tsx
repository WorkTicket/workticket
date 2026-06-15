import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatCard } from "../../components/dashboard/stat-card";
import { Camera } from "lucide-react";

describe("StatCard", () => {
  it("renders label and value", () => {
    render(<StatCard label="Total Jobs" value={42} />);
    expect(screen.getByText("Total Jobs")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("renders with string value", () => {
    render(<StatCard label="Revenue" value="$1,234" />);
    expect(screen.getByText("Revenue")).toBeInTheDocument();
    expect(screen.getByText("$1,234")).toBeInTheDocument();
  });

  it("renders icon when provided", () => {
    render(<StatCard label="Photos" value={10} icon={Camera} />);
    const icon = document.querySelector("svg");
    expect(icon).toBeInTheDocument();
    expect(icon).toHaveAttribute("aria-hidden", "true");
  });

  it("renders without icon when not provided", () => {
    const { container } = render(<StatCard label="Jobs" value={5} />);
    const icons = container.querySelectorAll("svg");
    expect(icons.length).toBe(0);
  });

  it("renders trend when provided", () => {
    render(<StatCard label="Growth" value="15%" trend="+5% from last month" />);
    expect(screen.getByText("+5% from last month")).toBeInTheDocument();
  });

  it("does not render trend when not provided", () => {
    render(<StatCard label="Total" value={100} />);
    expect(screen.queryByText("from last month")).not.toBeInTheDocument();
  });

  it("applies custom className", () => {
    const { container } = render(<StatCard label="Test" value={1} className="custom-class" />);
    expect(container.firstChild).toHaveClass("custom-class");
  });

  it("renders numeric zero correctly", () => {
    render(<StatCard label="Active" value={0} />);
    expect(screen.getByText("0")).toBeInTheDocument();
  });
});
