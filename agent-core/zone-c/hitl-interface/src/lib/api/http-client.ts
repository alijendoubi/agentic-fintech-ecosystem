import { backendErrorSchema, holdListSchema, signalIdParamSchema, signalSchema } from "@/lib/signals/schema";
import type { Signal } from "@/lib/signals/schema";
import { err, ok } from "./types";
import type { ApiErrorCode, DecisionSubmission, HitlApiClient, OperatorContext, Result } from "./types";

export interface HttpClientOptions {
  /** Server-side env only (HITL_API_BASE_URL). Never NEXT_PUBLIC. */
  readonly baseUrl: string;
  readonly timeoutMs: number;
  /** Optional service-to-service token (HITL_API_TOKEN), sent as X-Service-Token. */
  readonly serviceToken?: string | null;
  readonly fetchImpl?: typeof fetch;
}

const STATUS_TO_CODE: ReadonlyArray<readonly [(status: number) => boolean, ApiErrorCode]> = [
  [(s) => s === 401, "unauthorized"],
  [(s) => s === 403, "forbidden"],
  [(s) => s === 404, "not_found"],
  [(s) => s === 409 || s === 410 || s === 422, "refused"],
  [(s) => s === 429 || s === 503, "unavailable"],
];

function codeForStatus(status: number): ApiErrorCode {
  return STATUS_TO_CODE.find(([matches]) => matches(status))?.[1] ?? "server_error";
}

async function readErrorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const parsed = backendErrorSchema.safeParse(await response.json());
    return parsed.success ? parsed.data.error.message : fallback;
  } catch {
    return fallback;
  }
}

/**
 * HTTP implementation of the HITL backend contract (docs/api-contract.md).
 * Every failure (network, timeout, non-2xx, schema violation) becomes an
 * { ok: false } result; nothing here can produce an "approved" outcome by itself.
 */
export class HttpHitlApiClient implements HitlApiClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly serviceToken: string | null;
  private readonly fetchImpl: typeof fetch;

  constructor(options: HttpClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.timeoutMs = options.timeoutMs;
    this.serviceToken = options.serviceToken ?? null;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  listHolds(ctx: OperatorContext): Promise<Result<readonly Signal[]>> {
    return this.request("GET", "/v1/holds?status=pending", ctx, undefined, (body) => {
      const parsed = holdListSchema.safeParse(body);
      return parsed.success ? ok(parsed.data.holds) : err("invalid_response", "Unexpected hold list shape");
    });
  }

  getHold(holdId: string, ctx: OperatorContext): Promise<Result<Signal>> {
    const id = signalIdParamSchema.safeParse(holdId);
    if (!id.success) return Promise.resolve(err("not_found", "Invalid hold id"));
    return this.request("GET", `/v1/holds/${encodeURIComponent(id.data)}`, ctx, undefined, parseSignal);
  }

  submitDecision(submission: DecisionSubmission, ctx: OperatorContext): Promise<Result<Signal>> {
    const id = signalIdParamSchema.safeParse(submission.holdId);
    if (!id.success) return Promise.resolve(err("not_found", "Invalid hold id"));
    const body = {
      decision: submission.decision,
      reason: submission.reason,
      approver: submission.approver,
      clientRequestId: submission.clientRequestId,
    };
    return this.request("POST", `/v1/holds/${encodeURIComponent(id.data)}/decisions`, ctx, body, parseSignal);
  }

  private headers(ctx: OperatorContext, hasBody: boolean): Record<string, string> {
    return {
      accept: "application/json",
      authorization: `Bearer ${ctx.token}`,
      ...(hasBody ? { "content-type": "application/json" } : {}),
      ...(this.serviceToken ? { "x-service-token": this.serviceToken } : {}),
    };
  }

  private async request<T>(
    method: "GET" | "POST",
    path: string,
    ctx: OperatorContext,
    body: unknown,
    parse: (json: unknown) => Result<T>,
  ): Promise<Result<T>> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        headers: this.headers(ctx, body !== undefined),
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
        redirect: "error",
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (error) {
      const timedOut = error instanceof DOMException && (error.name === "TimeoutError" || error.name === "AbortError");
      return timedOut ? err("timeout", "Backend did not respond in time") : err("network", "Backend unreachable");
    }

    if (!response.ok) {
      return err(codeForStatus(response.status), await readErrorMessage(response, `Backend returned ${response.status}`));
    }
    try {
      return parse(await response.json());
    } catch {
      return err("invalid_response", "Backend returned a non-JSON body");
    }
  }
}

function parseSignal(json: unknown): Result<Signal> {
  const parsed = signalSchema.safeParse(json);
  return parsed.success ? ok(parsed.data) : err("invalid_response", "Unexpected hold shape");
}
