import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, HelpCircle, Sparkles, Wrench } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { useAuth } from "../auth/auth";
import { api, qs } from "./api";
import type { AttentionItem, Page } from "./api";
import { Badge, Card, Empty, ErrorNote, PageHeader, Pagination, Spinner, formatDateTime, titleCase, toast } from "./ui";
import type { Tone } from "./ui";

type Filter = "all" | AttentionItem["type"];

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "interested", label: "Interested" },
  { value: "question", label: "Questions" },
  { value: "channel_error", label: "Channel errors" },
  { value: "needs_manual", label: "Needs manual" },
];
const PAGE_SIZE = 20;

const KINDS: Record<AttentionItem["type"], { icon: LucideIcon; tone: Tone; label: string }> = {
  interested: { icon: Sparkles, tone: "green", label: "Interested" },
  question: { icon: HelpCircle, tone: "cyan", label: "Question" },
  channel_error: { icon: AlertTriangle, tone: "red", label: "Channel error" },
  needs_manual: { icon: Wrench, tone: "amber", label: "Needs manual" },
};

function text(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function Detail({ label, value }: { label: string; value: unknown }) {
  const shown = text(value);
  if (!shown) return null;
  return (
    <>
      <dt className="text-slate-500">{label}</dt>
      <dd className="break-words text-slate-200">{shown}</dd>
    </>
  );
}

function ItemCard({ item, onDone }: { item: AttentionItem; onDone: () => void }) {
  const { icon: Icon, tone, label } = KINDS[item.type];
  const payload = item.payload;
  const isQuestion = item.type === "question";
  const hasMessage = payload.message_id !== undefined && payload.message_id !== null;
  const [draft, setDraft] = useState(text(payload.draft));
  const [editing, setEditing] = useState(false);
  useEffect(() => setDraft(text(payload.draft)), [payload.draft]);

  const fail = (error: unknown) => toast(error instanceof Error ? error.message : "Action failed", "error");
  const approve = useMutation({
    mutationFn: () => api.post(`/attention/${item.id}/approve`),
    onSuccess: () => {
      toast(hasMessage ? "Approved: it sends on the next run" : "Marked as handled");
      onDone();
    },
    onError: fail,
  });
  const dismiss = useMutation({
    mutationFn: () => api.post(`/attention/${item.id}/dismiss`),
    onSuccess: () => {
      toast("Dismissed");
      onDone();
    },
    onError: fail,
  });
  const save = useMutation({
    mutationFn: () => api.patch(`/attention/${item.id}`, { draft }),
    onSuccess: () => {
      toast("Draft saved");
      setEditing(false);
      onDone();
    },
    onError: fail,
  });

  const { isMember } = useAuth(); // viewers are read-only
  const open = item.status === "open";
  const canResolve = open && isMember;
  const busy = approve.isPending || dismiss.isPending || save.isPending;
  const summary = text(payload.summary);

  return (
    <Card>
      <div className="flex flex-wrap items-center gap-2">
        <Icon aria-hidden="true" className="h-5 w-5 text-slate-300" />
        <Badge tone={tone}>{label}</Badge>
        {!open && <Badge>{titleCase(item.status)}</Badge>}
        <span className="ml-auto text-xs text-slate-500">{formatDateTime(item.created_at)}</span>
      </div>

      {summary && <p className="mt-3 whitespace-pre-wrap text-sm text-slate-200">{summary}</p>}

      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <Detail label="Reason" value={payload.reason} />
        <Detail label="Channel" value={payload.channel} />
        <Detail label="Job" value={payload.job} />
        <Detail label="Error" value={payload.error} />
        <Detail label="Name" value={payload.name} />
        <Detail label="Company" value={payload.company} />
        <Detail label="Email" value={payload.email} />
        <Detail label="Phone" value={payload.phone} />
        <Detail label="Interest" value={payload.interest} />
        <Detail label="Referral" value={payload.referral_name} />
        <Detail label={isQuestion ? "Their question" : "Message"} value={payload.question ?? payload.message ?? payload.text} />
      </dl>

      {isQuestion && (
        <div className="mt-4">
          <label htmlFor={`draft-${item.id}`} className="mb-1.5 block text-sm font-medium text-slate-300">
            Drafted answer
          </label>
          {editing && canResolve ? (
            <>
              <textarea id={`draft-${item.id}`} rows={5} value={draft} onChange={(event) => setDraft(event.target.value)} className="field" />
              <div className="mt-2 flex gap-2">
                <button type="button" className="btn-primary btn-sm" disabled={busy || !draft.trim()} onClick={() => save.mutate()}>
                  Save draft
                </button>
                <button
                  type="button"
                  className="btn-ghost btn-sm"
                  onClick={() => {
                    setEditing(false);
                    setDraft(text(payload.draft));
                  }}
                >
                  Discard
                </button>
              </div>
            </>
          ) : (
            <p id={`draft-${item.id}`} className="whitespace-pre-wrap rounded-xl border border-white/10 bg-white/[0.03] p-3 text-sm text-slate-200">
              {text(payload.draft) || "No draft."}
            </p>
          )}
        </div>
      )}

      {canResolve && !editing && (
        <div className="mt-4 flex flex-wrap gap-2">
          <button type="button" className="btn-primary btn-sm" disabled={busy} onClick={() => approve.mutate()}>
            <Check aria-hidden="true" className="h-4 w-4" />
            {hasMessage ? "Approve and send" : "Mark handled"}
          </button>
          {isQuestion && (
            <button type="button" className="btn-ghost btn-sm" disabled={busy} onClick={() => setEditing(true)}>
              Edit answer
            </button>
          )}
          <button type="button" className="btn-ghost btn-sm" disabled={busy} onClick={() => dismiss.mutate()}>
            Dismiss
          </button>
        </div>
      )}
    </Card>
  );
}

export default function Attention() {
  const client = useQueryClient();
  const [filter, setFilter] = useState<Filter>("all");
  const [page, setPage] = useState(1);
  useEffect(() => setPage(1), [filter]);

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["attention", filter, page],
    queryFn: () =>
      api.get<Page<AttentionItem>>(
        `/dashboard/attention${qs({ type: filter === "all" ? undefined : filter, status: "open", page, page_size: PAGE_SIZE })}`,
      ),
    refetchInterval: 15_000,
    placeholderData: (previous) => previous,
  });

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["attention"] });
    client.invalidateQueries({ queryKey: ["status"] });
    client.invalidateQueries({ queryKey: ["drafts"] });
  };

  return (
    <>
      <PageHeader title="Needs attention" />
      <div role="tablist" aria-label="Filter by type" className="-mx-4 mb-5 flex gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:px-0">
        {FILTERS.map((item) => (
          <button
            key={item.value}
            type="button"
            role="tab"
            aria-selected={filter === item.value}
            onClick={() => setFilter(item.value)}
            className={`whitespace-nowrap rounded-full border px-4 py-2 text-sm font-medium transition ${
              filter === item.value ? "border-cyan-glow/50 bg-cyan-glow/15 text-white" : "border-white/10 text-slate-400 hover:text-white"
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {isLoading && <Spinner />}
      {error && <ErrorNote error={error} retry={() => refetch()} />}
      {data && data.items.length === 0 && <Empty>All clear. Nothing needs you right now.</Empty>}
      {data && (
        <ul className="space-y-4">
          {data.items.map((item) => (
            <li key={item.id}>
              <ItemCard item={item} onDone={refresh} />
            </li>
          ))}
        </ul>
      )}
      {data && <Pagination page={data.page} pageSize={data.page_size} total={data.total} onChange={setPage} />}
    </>
  );
}
