import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useAuth } from "../auth/auth";
import { api, qs } from "./api";
import type { Draft, Page } from "./api";
import {
  Badge,
  Card,
  ChannelBadge,
  Countdown,
  Empty,
  ErrorNote,
  PageHeader,
  Pagination,
  Spinner,
  StatusBadge,
  toast,
} from "./ui";

type View = "awaiting" | "held" | "sent" | "closed" | "all";

const VIEWS: { value: View; label: string }[] = [
  { value: "awaiting", label: "Awaiting approval" },
  { value: "held", label: "On hold" },
  { value: "sent", label: "Sent" },
  { value: "closed", label: "Cancelled" },
  { value: "all", label: "All" },
];
const PAGE_SIZE = 20;

function actionable(draft: Draft): boolean {
  if (draft.status === "drafted") return true;
  return draft.status === "approved" && (draft.review_status === "pending_review" || draft.review_status === "edited");
}

interface DraftCardProps {
  draft: Draft;
  selected: boolean;
  onSelect: (checked: boolean) => void;
  onDone: () => void;
}

function DraftCard({ draft, selected, onSelect, onDone }: DraftCardProps) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(draft.text);
  useEffect(() => setText(draft.text), [draft.text]);

  const fail = (error: unknown) => toast(error instanceof Error ? error.message : "Action failed", "error");
  const approve = useMutation({
    mutationFn: () => api.post<{ result: string }>(`/dashboard/drafts/${draft.id}/approve`),
    onSuccess: (result) => {
      toast(result.result === "released" ? "Released: it sends on the next run" : "Approved: the hold starts at the next queue run");
      onDone();
    },
    onError: fail,
  });
  const cancel = useMutation({
    mutationFn: () => api.post(`/dashboard/drafts/${draft.id}/cancel`),
    onSuccess: () => {
      toast("Cancelled. It will not be sent.");
      onDone();
    },
    onError: fail,
  });
  const save = useMutation({
    mutationFn: () => api.patch<{ result: string }>(`/dashboard/drafts/${draft.id}`, { text }),
    onSuccess: (result) => {
      toast(result.result === "hold_restarted" ? "Saved. The hold restarted and a new preview went to WhatsApp." : "Saved");
      setEditing(false);
      onDone();
    },
    onError: fail,
  });

  const { isMember } = useAuth(); // viewers are read-only
  const canAct = actionable(draft) && isMember;
  const busy = approve.isPending || cancel.isPending || save.isPending;
  const held = draft.status === "approved";

  return (
    <Card className={selected ? "ring-2 ring-cyan-glow/50" : ""}>
      <div className="flex items-start gap-3">
        {canAct && (
          <input
            type="checkbox"
            checked={selected}
            onChange={(event) => onSelect(event.target.checked)}
            aria-label={`Select draft for ${draft.person}`}
            className="mt-1.5 h-5 w-5 shrink-0 accent-cyan-400"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <p className="font-semibold text-white">{draft.person}</p>
            <span className="text-sm text-slate-400">{draft.company ?? "Unknown company"}</span>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <ChannelBadge channel={draft.channel} />
            <StatusBadge value={draft.review_status && held ? draft.review_status : draft.status} />
            {draft.is_reply && <Badge tone="violet">Reply</Badge>}
            {draft.edited && <Badge tone="violet">Edited</Badge>}
            {held && draft.review_status !== "cancelled" && (
              <span className="text-sm">
                <Countdown until={draft.hold_until} />
              </span>
            )}
          </div>

          {editing ? (
            <div className="mt-3">
              <label htmlFor={`draft-${draft.id}`} className="sr-only">
                Message text
              </label>
              <textarea
                id={`draft-${draft.id}`}
                rows={6}
                value={text}
                onChange={(event) => setText(event.target.value)}
                className="field"
              />
              <div className="mt-2 flex gap-2">
                <button type="button" className="btn-primary btn-sm" disabled={busy || !text.trim()} onClick={() => save.mutate()}>
                  Save
                </button>
                <button
                  type="button"
                  className="btn-ghost btn-sm"
                  onClick={() => {
                    setEditing(false);
                    setText(draft.text);
                  }}
                >
                  Discard
                </button>
              </div>
              {held && <p className="mt-2 text-xs text-slate-500">Saving restarts the hold and sends a new preview to WhatsApp.</p>}
            </div>
          ) : (
            <p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-slate-300">{draft.text}</p>
          )}

          {canAct && !editing && (
            <div className="mt-4 flex flex-wrap gap-2">
              <button type="button" className="btn-primary btn-sm" disabled={busy} onClick={() => approve.mutate()}>
                <Check aria-hidden="true" className="h-4 w-4" />
                {held || draft.is_reply ? "Release now" : "Approve"}
              </button>
              <button type="button" className="btn-ghost btn-sm" disabled={busy} onClick={() => setEditing(true)}>
                <Pencil aria-hidden="true" className="h-4 w-4" />
                Edit
              </button>
              <button
                type="button"
                className="btn-danger btn-sm"
                disabled={busy}
                onClick={() => window.confirm("Cancel this message? It will never be sent.") && cancel.mutate()}
              >
                <X aria-hidden="true" className="h-4 w-4" />
                Cancel
              </button>
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

export default function Drafts() {
  const client = useQueryClient();
  const { isMember } = useAuth();
  const [view, setView] = useState<View>("awaiting");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  useEffect(() => {
    setPage(1);
    setSelected(new Set());
  }, [view]);

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["drafts", view, page],
    queryFn: () => api.get<Page<Draft>>(`/dashboard/drafts${qs({ view, page, page_size: PAGE_SIZE })}`),
    refetchInterval: 15_000, // WhatsApp replies change these rows too
    placeholderData: (previous) => previous,
  });

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["drafts"] });
    client.invalidateQueries({ queryKey: ["overview"] });
    client.invalidateQueries({ queryKey: ["status"] });
  };

  const bulk = useMutation({
    mutationFn: (ids: number[]) =>
      api.post<{ approved: number[]; skipped: { id: number; reason: string }[] }>("/dashboard/drafts/bulk-approve", { ids }),
    onSuccess: (result) => {
      const skipped = result.skipped.length;
      toast(`Approved ${result.approved.length}${skipped ? `, skipped ${skipped}` : ""}`, skipped ? "error" : "ok");
      setSelected(new Set());
      refresh();
    },
    onError: (err) => toast(err instanceof Error ? err.message : "Bulk approve failed", "error"),
  });

  const selectable = isMember ? (data?.items ?? []).filter(actionable) : [];
  const allSelected = selectable.length > 0 && selectable.every((draft) => selected.has(draft.id));

  const toggle = (id: number, checked: boolean) =>
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });

  return (
    <>
      <PageHeader title="Drafts" />
      <div role="tablist" aria-label="Draft views" className="-mx-4 mb-5 flex gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:px-0">
        {VIEWS.map((item) => (
          <button
            key={item.value}
            type="button"
            role="tab"
            aria-selected={view === item.value}
            onClick={() => setView(item.value)}
            className={`whitespace-nowrap rounded-full border px-4 py-2 text-sm font-medium transition ${
              view === item.value ? "border-cyan-glow/50 bg-cyan-glow/15 text-white" : "border-white/10 text-slate-400 hover:text-white"
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {selectable.length > 0 && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3">
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={(event) => setSelected(event.target.checked ? new Set(selectable.map((d) => d.id)) : new Set())}
              className="h-5 w-5 accent-cyan-400"
            />
            Select all on this page
          </label>
          <button
            type="button"
            className="btn-primary btn-sm"
            disabled={selected.size === 0 || bulk.isPending}
            onClick={() => bulk.mutate([...selected])}
          >
            Approve selected ({selected.size})
          </button>
        </div>
      )}

      {isLoading && <Spinner />}
      {error && <ErrorNote error={error} retry={() => refetch()} />}
      {data && data.items.length === 0 && <Empty>Nothing here.</Empty>}
      {data && (
        <ul className="space-y-4">
          {data.items.map((draft) => (
            <li key={draft.id}>
              <DraftCard draft={draft} selected={selected.has(draft.id)} onSelect={(checked) => toggle(draft.id, checked)} onDone={refresh} />
            </li>
          ))}
        </ul>
      )}
      {data && <Pagination page={data.page} pageSize={data.page_size} total={data.total} onChange={setPage} />}
    </>
  );
}
