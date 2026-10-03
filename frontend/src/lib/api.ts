import type { InquiryPayload } from "../types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

const FALLBACK = "Something went wrong. Please try again in a moment.";

async function readDetail(response: Response): Promise<string | undefined> {
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string") return detail;
    }
  } catch {
    /* non-JSON error body */
  }
  return undefined;
}

export async function submitInquiry(payload: InquiryPayload): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/api/inquiries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch {
    throw new ApiError(0, "Could not reach the server. Check your connection and try again.");
  }
  if (response.ok) return;
  if (response.status === 429) {
    throw new ApiError(429, "Too many requests. Please wait a while and try again.");
  }
  throw new ApiError(response.status, (await readDetail(response)) ?? FALLBACK);
}
