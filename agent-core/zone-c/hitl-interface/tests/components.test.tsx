// @vitest-environment jsdom
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DecisionForm } from "@/components/DecisionForm";
import { ExpiryCountdown } from "@/components/ExpiryCountdown";
import { SignalDetail } from "@/components/SignalDetail";
import { NOW_MS, makeSignal } from "./helpers/fixtures";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

const GOOD_REASON = "Reviewed debate trace, sizing within limits.";

function form(overrides: Partial<React.ComponentProps<typeof DecisionForm>> = {}) {
  return (
    <DecisionForm
      holdId="hold-test-001"
      csrfToken="csrf-abc"
      blockedReason={null}
      expiresAtMs={Date.now() + 600_000}
      serverNowMs={Date.now()}
      requiredApprovals={1}
      approvalsSoFar={0}
      {...overrides}
    />
  );
}

function mockFetch(impl: () => Promise<Response>) {
  const fn = vi.fn(impl);
  vi.stubGlobal("fetch", fn);
  return fn;
}

const okResponse = () => Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));

beforeEach(() => refresh.mockClear());
afterEach(() => vi.unstubAllGlobals());

describe("DecisionForm", () => {
  it("requires a reason before anything is sent", async () => {
    const fetchFn = mockFetch(okResponse);
    render(form());
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(fetchFn).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/reason of at least 10 characters is mandatory/i);
  });

  it("posts the approval with the CSRF header and shows confirmation only after the backend confirms", async () => {
    const fetchFn = mockFetch(okResponse);
    render(form());
    await userEvent.type(screen.getByLabelText(/reason/i), GOOD_REASON);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Approval recorded."));
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/holds/hold-test-001/decision");
    expect(init.headers).toMatchObject({ "x-csrf-token": "csrf-abc" });
    expect(JSON.parse(String(init.body))).toEqual({ decision: "approve", reason: GOOD_REASON });
    expect(refresh).toHaveBeenCalled();
  });

  it("posts a rejection", async () => {
    const fetchFn = mockFetch(okResponse);
    render(form());
    await userEvent.type(screen.getByLabelText(/reason/i), GOOD_REASON);
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Rejection recorded."));
    expect(JSON.parse(String((fetchFn.mock.calls[0] as unknown as [string, RequestInit])[1].body)).decision).toBe("reject");
  });

  it("shows NOT approved when the backend denies", async () => {
    mockFetch(() => Promise.resolve(new Response(JSON.stringify({ ok: false, message: "Signal expired." }), { status: 409 })));
    render(form());
    await userEvent.type(screen.getByLabelText(/reason/i), GOOD_REASON);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Not approved\. Signal expired\./));
    expect(refresh).not.toHaveBeenCalled();
  });

  it.each([
    ["network failure", () => Promise.reject(new TypeError("fetch failed"))],
    ["timeout", () => Promise.reject(new DOMException("timeout", "TimeoutError"))],
    ["non-JSON success", () => Promise.resolve(new Response("<html>", { status: 200 }))],
    ["ok status but no ok:true", () => Promise.resolve(new Response(JSON.stringify({ ok: false }), { status: 200 }))],
  ])("treats %s as NOT approved (deny by default)", async (_name, impl) => {
    mockFetch(impl);
    render(form());
    await userEvent.type(screen.getByLabelText(/reason/i), GOOD_REASON);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Not approved\./));
    expect(screen.queryByText("Approval recorded.")).not.toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("disables everything with the server-provided reason for viewers", () => {
    render(form({ blockedReason: "Viewers cannot approve or reject signals." }));
    expect(screen.getByRole("alert")).toHaveTextContent("Viewers cannot approve");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(screen.getByLabelText(/reason/i)).toBeDisabled();
  });

  it("disables actions when the signal has already expired", () => {
    render(form({ expiresAtMs: Date.now() - 1000 }));
    expect(screen.getByRole("alert")).toHaveTextContent(/expired/i);
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("explains four-eyes progress", () => {
    render(form({ requiredApprovals: 2, approvalsSoFar: 1 }));
    expect(screen.getByText(/1 of 2 required/)).toBeInTheDocument();
    expect(screen.getByText(/two different approvers/)).toBeInTheDocument();
  });
});

describe("ExpiryCountdown", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("counts down and flips to expired without a reload", () => {
    const start = Date.now();
    render(<ExpiryCountdown expiresAtMs={start + 3000} serverNowMs={start} />);
    expect(screen.getByRole("timer")).toHaveTextContent("00:03");
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(screen.getByRole("status")).toHaveTextContent(/Expired/);
  });

  it("uses server time, not the operator's skewed clock", () => {
    const serverNow = Date.now() + 10 * 60_000;
    render(<ExpiryCountdown expiresAtMs={serverNow - 1} serverNowMs={serverNow} />);
    expect(screen.getByRole("status")).toHaveTextContent(/Expired/);
  });
});

describe("SignalDetail", () => {
  it("shows the persistent AI-generated label and the key fields", () => {
    render(<SignalDetail signal={makeSignal()} status="PENDING" required={1} serverNowMs={NOW_MS} />);
    expect(screen.getByText("AI-generated signal")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("BUY 100.0000 TESTCO");
    expect(screen.getByText("Omega (judge confidence)")).toBeInTheDocument();
    expect(screen.getByText("TEST blue case")).toBeInTheDocument();
    expect(screen.getByText("TEST red case")).toBeInTheDocument();
    expect(screen.getByText("TEST judge verdict")).toBeInTheDocument();
    expect(screen.getByText("Pending decision")).toBeInTheDocument();
    expect(screen.getByText(/unusual order size/i)).toBeInTheDocument();
  });

  it("renders decision history with approver identity and reason", () => {
    const signal = makeSignal({
      approvals: [{ approverSub: "approver-a", decision: "APPROVE", reason: "Looks right to me", decidedAtNs: "1758369600000000000" }],
    });
    render(<SignalDetail signal={signal} status="AWAITING_SECOND_APPROVER" required={2} serverNowMs={NOW_MS} />);
    expect(screen.getByText(/Approved by approver-a/)).toHaveTextContent("Looks right to me");
    expect(screen.getByText("Awaiting second approver")).toBeInTheDocument();
  });
});
