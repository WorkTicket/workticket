import { describe, it, expect, beforeEach } from "vitest";
import { useOnboardingStore, onboardingProgress } from "../../stores/onboarding-store";

beforeEach(() => {
  useOnboardingStore.setState({
    steps: [
      { id: "company", label: "Create company", completed: false },
      { id: "customer", label: "Add first customer", completed: false },
      { id: "job", label: "Create first job", completed: false },
      { id: "photo", label: "Upload first photo", completed: false },
      { id: "invite", label: "Invite first employee", completed: false },
    ],
    dismissed: false,
  });
});

describe("useOnboardingStore", () => {
  it("initializes with default steps and not dismissed", () => {
    const state = useOnboardingStore.getState();
    expect(state.steps).toHaveLength(5);
    expect(state.dismissed).toBe(false);
    expect(state.steps.every((s) => !s.completed)).toBe(true);
  });

  it("completeStep marks a step as completed", () => {
    useOnboardingStore.getState().completeStep("company");
    const state = useOnboardingStore.getState();
    expect(state.steps.find((s) => s.id === "company")?.completed).toBe(true);
    expect(state.steps.find((s) => s.id === "customer")?.completed).toBe(false);
  });

  it("completeStep does not affect other steps", () => {
    useOnboardingStore.getState().completeStep("company");
    useOnboardingStore.getState().completeStep("customer");
    const state = useOnboardingStore.getState();
    expect(state.steps.filter((s) => s.completed)).toHaveLength(2);
  });

  it("completeStep for unknown id does nothing", () => {
    useOnboardingStore.getState().completeStep("nonexistent");
    const state = useOnboardingStore.getState();
    expect(state.steps.every((s) => !s.completed)).toBe(true);
  });

  it("dismiss sets dismissed to true", () => {
    useOnboardingStore.getState().dismiss();
    expect(useOnboardingStore.getState().dismissed).toBe(true);
  });

  it("dismiss does not affect steps", () => {
    useOnboardingStore.getState().completeStep("company");
    useOnboardingStore.getState().dismiss();
    const state = useOnboardingStore.getState();
    expect(state.dismissed).toBe(true);
    expect(state.steps.find((s) => s.id === "company")?.completed).toBe(true);
  });

  it("reset clears all progress", () => {
    useOnboardingStore.getState().completeStep("company");
    useOnboardingStore.getState().completeStep("customer");
    useOnboardingStore.getState().dismiss();
    useOnboardingStore.getState().reset();
    const state = useOnboardingStore.getState();
    expect(state.dismissed).toBe(false);
    expect(state.steps.every((s) => !s.completed)).toBe(true);
  });

  it("reset restores default steps", () => {
    useOnboardingStore.getState().completeStep("company");
    useOnboardingStore.getState().reset();
    const state = useOnboardingStore.getState();
    expect(state.steps).toHaveLength(5);
    expect(state.steps[0].id).toBe("company");
  });
});

describe("onboardingProgress", () => {
  it("returns 0 when no steps completed", () => {
    const steps = [
      { id: "a", label: "A", completed: false },
      { id: "b", label: "B", completed: false },
    ];
    expect(onboardingProgress(steps)).toBe(0);
  });

  it("returns 100 when all steps completed", () => {
    const steps = [
      { id: "a", label: "A", completed: true },
      { id: "b", label: "B", completed: true },
    ];
    expect(onboardingProgress(steps)).toBe(100);
  });

  it("returns 50 when half steps completed", () => {
    const steps = [
      { id: "a", label: "A", completed: true },
      { id: "b", label: "B", completed: false },
    ];
    expect(onboardingProgress(steps)).toBe(50);
  });

  it("rounds to nearest integer", () => {
    const steps = [
      { id: "a", label: "A", completed: true },
      { id: "b", label: "B", completed: false },
      { id: "c", label: "C", completed: false },
    ];
    expect(onboardingProgress(steps)).toBe(33);
  });

  it("handles empty steps array", () => {
    expect(onboardingProgress([])).toBe(0);
  });
});
