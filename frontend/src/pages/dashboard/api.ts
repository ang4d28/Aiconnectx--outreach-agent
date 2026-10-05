export const LOGOUT_EVENT = "connectx:logout";

// The session lives in an httpOnly cookie set by the backend; nothing is stored in the browser.
// State-changing requests must carry this header (CSRF protection on the backend).
const CSRF_HEADER = "X-CSRF-Protection";

export class HttpError extends Error {
  constructor(
    public status: number,
    message: string,
    public fieldErrors: Record<string, string> = {},
  ) {
    super(message);
  }
}

function describe(detail: unknown): { message: string; fields: Record<string, string> } {
  if (typeof detail === "string") return { message: detail, fields: {} };
  if (detail && typeof detail === "object" && "errors" in detail) {
    const fields = (detail as { errors: Record<string, string> }).errors;
    const text = Object.entries(fields)
      .map(([key, value]) => `${key.replace(/_/g, " ")} ${value}`)
      .join("; ");
    return { message: text, fields };
  }
  if (Array.isArray(detail)) {
    const fields: Record<string, string> = {};
    for (const item of detail as { loc?: unknown[]; msg?: string }[]) {
      const key = String(item?.loc?.[item.loc.length - 1] ?? "");
      if (key && key !== "body" && !fields[key]) fields[key] = String(item?.msg ?? "is invalid").replace(/^Value error, /, "");
    }
    return { message: detail.map((item) => String(item?.msg ?? item).replace(/^Value error, /, "")).join("; "), fields };
  }
  return { message: "Something went wrong. Please try again.", fields: {} };
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") headers[CSRF_HEADER] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new HttpError(0, "Could not reach the server.");
  }
  // The session ended (expired, password reset, ...): everything signed-in goes back to the login page.
  if (response.status === 401 && !path.startsWith("/auth/")) window.dispatchEvent(new Event(LOGOUT_EVENT));
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail;
    } catch {
      detail = undefined;
    }
    const { message, fields } = describe(detail);
    throw new HttpError(response.status, message, fields);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

/** Fetch a file with the session cookie and return it as a Blob (a plain link could not carry the cookie check's errors). */
async function download(path: string): Promise<{ blob: Blob; filename: string }> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { credentials: "same-origin" });
  } catch {
    throw new HttpError(0, "Could not reach the server.");
  }
  if (response.status === 401) window.dispatchEvent(new Event(LOGOUT_EVENT));
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail;
    } catch {
      detail = undefined;
    }
    throw new HttpError(response.status, describe(detail).message);
  }
  const match = /filename="?([^";]+)"?/.exec(response.headers.get("Content-Disposition") ?? "");
  return { blob: await response.blob(), filename: match?.[1] ?? "download" };
}

export function qs(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  post: <T,>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  patch: <T,>(path: string, body: unknown) => request<T>("PATCH", path, body),
  put: <T,>(path: string, body: unknown) => request<T>("PUT", path, body),
  download,
};

// ─── Types ─────────────────────────────────────────────────

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export type Channel = "linkedin" | "email";

export type InquiryStatus = "new" | "contacted" | "closed";

export interface Inquiry {
  id: number;
  name: string;
  email: string;
  company: string | null;
  phone: string | null;
  interest: string;
  message: string;
  status: InquiryStatus;
  notes: string;
  created_at: string | null;
}

export interface DashboardStatus {
  paused: boolean;
  dry_run: boolean;
  disabled_channels: { channel: string; reason: string | null }[];
  linkedin_needs_login: boolean;
  open_attention: Record<string, number>;
}

export type ChannelState = "connected" | "needs_login" | "disabled" | "error" | "unknown";

export interface ChannelStatus {
  channel: "linkedin" | "outlook" | "whatsapp";
  label: string;
  state: ChannelState;
  account_name: string | null;
  profile_url: string | null;
  last_checked_at: string | null;
  detail: string | null;
}

export interface ChannelsPayload {
  channels: ChannelStatus[];
  linkedin_login_running: boolean;
  display_available: boolean;
  login_command: string;
}

export interface Overview {
  cards: {
    leads_today: number;
    drafts_pending: number;
    sent_today: number;
    replies_today: number;
    interested_today: number;
  };
  activity: { date: string; leads: number; sent: number; replies: number }[];
  funnel: { stage: string; count: number }[];
  caps: { sent: Record<string, number>; limits: Record<string, number> };
}

export interface Lead {
  id: number;
  person: string;
  title: string | null;
  email: string | null;
  linkedin_url: string | null;
  company: string | null;
  channel: Channel;
  status: string;
  source: "post" | "search";
  post_url: string | null;
  sent_at: string | null;
  follow_up_due: string | null;
  created_at: string | null;
}

export interface ThreadMessage {
  id: number;
  direction: "inbound" | "outbound";
  text: string;
  status: string;
  review_status: string | null;
  at: string | null;
}

export interface LeadDetail extends Lead {
  follow_up_count: number;
  post: { url: string; roles: string[]; text: string; relevance: number; poster: string | null } | null;
  messages: ThreadMessage[];
}

export interface Draft {
  id: number;
  outreach_id: number;
  person: string;
  company: string | null;
  channel: Channel;
  status: string;
  review_status: string | null;
  text: string;
  edited: boolean;
  hold_until: string | null;
  is_reply: boolean;
  created_at: string | null;
}

export interface ReplyRow {
  outreach_id: number;
  message_id: number;
  person: string;
  company: string | null;
  channel: Channel;
  text: string;
  received_at: string | null;
  status: string;
  intent: string;
}

export interface AttentionItem {
  id: number;
  type: "interested" | "question" | "channel_error" | "needs_manual";
  status: "open" | "resolved" | "dismissed";
  payload: Record<string, unknown>;
  created_at: string | null;
}

export interface SettingsValues {
  linkedin_daily_cap: number;
  email_daily_cap: number;
  send_window_start: string;
  send_window_end: string;
  hold_window_minutes: number;
  dry_run: boolean;
  tpo_name: string;
  tpo_phone: string;
  tpo_email: string;
}

export interface SettingsPayload {
  values: SettingsValues;
  search_queries: string[];
}

export interface SheetInfo {
  configured: boolean;
  url: string | null;
  last_synced_at: string | null;
}
