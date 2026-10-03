import { ChevronLeft, ChevronRight, Loader2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

// ─── Formatting ────────────────────────────────────────────

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  return new Date(value).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

// ─── Layout pieces ─────────────────────────────────────────

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
      <h1 className="text-2xl font-bold tracking-tight text-white sm:text-3xl">{title}</h1>
      {children}
    </div>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`glass rounded-2xl p-4 shadow-card sm:p-5 ${className}`}>{children}</div>;
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center justify-center gap-2 py-12 text-slate-400">
      <Loader2 aria-hidden="true" className="h-5 w-5 animate-spin motion-reduce:animate-none" />
      <span>{label}...</span>
    </div>
  );
}

export function ErrorNote({ error, retry }: { error: unknown; retry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  return (
    <div role="alert" className="rounded-xl border border-rose-400/30 bg-rose-500/10 p-4 text-sm text-rose-200">
      {message}
      {retry && (
        <button type="button" onClick={retry} className="ml-3 font-semibold underline">
          Try again
        </button>
      )}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="rounded-2xl border border-dashed border-white/10 py-12 text-center text-slate-400">{children}</p>;
}

// ─── Badges ────────────────────────────────────────────────

const TONES = {
  slate: "border-white/15 bg-white/5 text-slate-300",
  cyan: "border-cyan-glow/30 bg-cyan-glow/10 text-cyan-200",
  violet: "border-violet-glow/40 bg-violet-glow/15 text-violet-200",
  green: "border-emerald-400/30 bg-emerald-400/10 text-emerald-200",
  amber: "border-amber-400/30 bg-amber-400/10 text-amber-200",
  red: "border-rose-400/30 bg-rose-500/10 text-rose-200",
} as const;

export type Tone = keyof typeof TONES;

export function Badge({ tone = "slate", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${TONES[tone]}`}>
      {children}
    </span>
  );
}

export function ChannelBadge({ channel }: { channel: string }) {
  return <Badge tone={channel === "linkedin" ? "cyan" : "violet"}>{channel === "linkedin" ? "LinkedIn" : "Email"}</Badge>;
}

const STATUS_TONES: Record<string, Tone> = {
  interested: "green",
  replied: "cyan",
  unsubscribed: "red",
  closed: "slate",
  failed: "red",
  rejected: "red",
  cancelled: "red",
  sent: "green",
  approved: "cyan",
  pending_review: "amber",
  edited: "violet",
  auto_released: "green",
  drafted: "slate",
  no_opportunity: "slate",
  future_pool: "violet",
  not_now: "amber",
  wrong_person: "amber",
  question: "cyan",
  other: "slate",
  unsubscribe: "red",
};

export function StatusBadge({ value }: { value: string | null }) {
  if (!value) return null;
  return <Badge tone={STATUS_TONES[value] ?? "slate"}>{titleCase(value)}</Badge>;
}

// ─── Pagination ────────────────────────────────────────────

interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onChange: (page: number) => void;
}

export function Pagination({ page, pageSize, total, onChange }: PaginationProps) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return null;
  return (
    <nav aria-label="Pagination" className="mt-6 flex items-center justify-between gap-3 text-sm text-slate-400">
      <span>
        Page {page} of {pages} ({total} total)
      </span>
      <span className="flex gap-2">
        <button
          type="button"
          className="btn-ghost btn-sm"
          disabled={page <= 1}
          onClick={() => onChange(page - 1)}
          aria-label="Previous page"
        >
          <ChevronLeft aria-hidden="true" className="h-4 w-4" />
        </button>
        <button
          type="button"
          className="btn-ghost btn-sm"
          disabled={page >= pages}
          onClick={() => onChange(page + 1)}
          aria-label="Next page"
        >
          <ChevronRight aria-hidden="true" className="h-4 w-4" />
        </button>
      </span>
    </nav>
  );
}

// ─── Countdown ─────────────────────────────────────────────

export function Countdown({ until }: { until: string | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  if (!until) return <span className="text-slate-400">Hold not started</span>;
  const seconds = Math.ceil((new Date(until).getTime() - now) / 1000);
  if (seconds <= 0) return <span className="text-emerald-300">Released, sends in the next window</span>;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = String(seconds % 60).padStart(2, "0");
  const text = hours > 0 ? `${hours}h ${String(minutes).padStart(2, "0")}m` : `${minutes}:${rest}`;
  return (
    <span className="text-amber-300" aria-label={`Sends in ${text}`}>
      Sends in {text}
    </span>
  );
}

// ─── Drawer ────────────────────────────────────────────────

export function Drawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKey);
      previousFocus?.focus();
    };
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-[60]">
      <div className="absolute inset-0 bg-navy-950/70 backdrop-blur-sm" onClick={onClose} aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="absolute inset-y-0 right-0 flex w-full max-w-xl flex-col border-l border-white/10 bg-navy-900 shadow-2xl"
      >
        <div className="flex items-center justify-between border-b border-white/10 px-5 py-4">
          <h2 className="truncate text-lg font-semibold text-white">{title}</h2>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="-mr-2 inline-flex h-10 w-10 items-center justify-center rounded-lg text-slate-300 hover:bg-white/10"
          >
            <X aria-hidden="true" className="h-5 w-5" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-5">{children}</div>
      </div>
    </div>
  );
}

// ─── Toasts ────────────────────────────────────────────────

type ToastTone = "ok" | "error";
interface ToastItem {
  id: number;
  text: string;
  tone: ToastTone;
}

const listeners = new Set<(toast: ToastItem) => void>();
let toastId = 0;

export function toast(text: string, tone: ToastTone = "ok"): void {
  const item = { id: ++toastId, text, tone };
  listeners.forEach((listener) => listener(item));
}

export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>([]);
  useEffect(() => {
    const listener = (item: ToastItem) => {
      setItems((current) => [...current, item]);
      window.setTimeout(() => setItems((current) => current.filter((t) => t.id !== item.id)), 4000);
    };
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return (
    <div aria-live="polite" className="pointer-events-none fixed inset-x-0 bottom-20 z-[70] flex flex-col items-center gap-2 px-4 md:bottom-6">
      {items.map((item) => (
        <p
          key={item.id}
          className={`pointer-events-auto rounded-xl border px-4 py-2 text-sm shadow-card backdrop-blur-md ${
            item.tone === "error"
              ? "border-rose-400/40 bg-rose-950/90 text-rose-100"
              : "border-cyan-glow/30 bg-navy-800/95 text-white"
          }`}
        >
          {item.text}
        </p>
      ))}
    </div>
  );
}
