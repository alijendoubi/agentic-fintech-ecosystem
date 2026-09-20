import type { DisplayStatus } from "@/lib/signals/policy";

const LABELS: Readonly<Record<DisplayStatus, string>> = {
  PENDING: "Pending decision",
  AWAITING_SECOND_APPROVER: "Awaiting second approver",
  APPROVED: "Approved (released to Aegis)",
  REJECTED: "Rejected (final)",
  RELEASE_DENIED: "Approved, then denied by Aegis controls",
  EXPIRED: "Expired",
};

export function StatusBadge({ status }: { readonly status: DisplayStatus }) {
  return <span className={`badge badge-${status.toLowerCase()}`}>{LABELS[status]}</span>;
}
