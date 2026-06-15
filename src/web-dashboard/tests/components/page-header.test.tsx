import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { PageHeader } from "../../components/dashboard/page-header";

describe("PageHeader", () => {
  it("renders title", () => {
    render(<PageHeader title="Dashboard" />);
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
  });

  it("renders description when provided", () => {
    render(<PageHeader title="Dashboard" description="Overview of your account" />);
    expect(screen.getByText("Overview of your account")).toBeInTheDocument();
  });

  it("does not render description paragraph when not provided", () => {
    render(<PageHeader title="Dashboard" />);
    expect(screen.queryByText("Overview")).not.toBeInTheDocument();
  });

  it("renders action buttons when provided", () => {
    render(
      <PageHeader
        title="Jobs"
        actions={<button>New Job</button>}
      />,
    );
    expect(screen.getByText("New Job")).toBeInTheDocument();
  });

  it("renders both description and actions", () => {
    render(
      <PageHeader
        title="Estimates"
        description="Manage your estimates"
        actions={<button>Create</button>}
      />,
    );
    expect(screen.getByText("Estimates")).toBeInTheDocument();
    expect(screen.getByText("Manage your estimates")).toBeInTheDocument();
    expect(screen.getByText("Create")).toBeInTheDocument();
  });
});
