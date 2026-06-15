import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EmptyState } from "../../components/dashboard/empty-state";
import { Camera } from "lucide-react";

describe("EmptyState", () => {
  it("renders title", () => {
    render(<EmptyState title="No jobs yet" />);
    expect(screen.getByText("No jobs yet")).toBeInTheDocument();
  });

  it("renders description when provided", () => {
    render(
      <EmptyState title="No jobs" description="Create your first job to get started" />,
    );
    expect(screen.getByText("Create your first job to get started")).toBeInTheDocument();
  });

  it("renders icon when provided", () => {
    render(<EmptyState title="Empty" icon={Camera} />);
    const icon = document.querySelector("svg");
    expect(icon).toBeInTheDocument();
  });

  it("renders action button when provided", () => {
    render(
      <EmptyState
        title="No customers"
        actionLabel="Add Customer"
        onAction={() => {}}
      />,
    );
    expect(screen.getByText("Add Customer")).toBeInTheDocument();
  });

  it("calls onAction when button clicked", async () => {
    const onAction = vi.fn();
    render(
      <EmptyState title="No data" actionLabel="Create" onAction={onAction} />,
    );
    await userEvent.click(screen.getByText("Create"));
    expect(onAction).toHaveBeenCalledOnce();
  });

  it("does not render action button when not provided", () => {
    render(<EmptyState title="Nothing here" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders without description gracefully", () => {
    render(<EmptyState title="Just a title" />);
    expect(screen.getByText("Just a title")).toBeInTheDocument();
  });
});
