import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useAuth } from "../auth/auth";
import { api, qs } from "./api";
import type { Inquiry, InquiryStatus, Page } from "./api";
import { Badge, Drawer, Empty, ErrorNote, PageHeader, Pagination, Spinner, formatDateTime, titleCase, toast } from "./ui";
import type { Tone } from "./ui";

type Filter = "all" | InquiryStatus;

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "new", label: "New" },
  { value: "contacted", label: "Contacted" },
  { value: "closed", label: "Closed" },
];
const STATUS_TONES: Record<InquiryStatus, Tone> = { new: "amber", contacted: "cyan", closed: "slate" };
const PAGE_SIZE = 20;

function StatusPill({ value }: { value: InquiryStatus }) {
  return <Badge tone={STATUS_TONES[value]}>{titleCase(value)}</Badge>;
}

function snippet(message: string): string {
  return message.length > 80 ? `${message.slice(0, 80)}…` : message;
}

function InquiryDrawer({ inquiry, onClose }: { inquiry: Inquiry; onClose: () => void }) {
  const client = useQueryClient();
  const { isMember } = useAuth(); // viewers are read-only
  const [status, setStatus] = useState<InquiryStatus>(inquiry.status);
  const [notes, setNotes] = useState(inquiry.notes);
  useEffect(() => {
    setStatus(inquiry.status);
    setNotes(inquiry.notes);
  }, [inquiry.id, inquiry.status, inquiry.notes]);

  const save = useMutation({
    mutationFn: () => api.patch<Inquiry>(`/inquiries/${inquiry.id}`, { status, notes }),
    onSuccess: () => {
      toast("Inquiry saved");
      void client.invalidateQueries({ queryKey: ["inquiries"] });
    },
    onError: (error) => toast(error instanceof Error ? error.message : "Could not save", "error"),
  });
  const dirty = status !== inquiry.status || notes.trim() !== inquiry.notes;

  return (
    <Drawer title={inquiry.name} onClose={onClose}>
      <div className="space-y-6">
        <section aria-label="Contact">
          <div className="flex flex-wrap gap-2">
            <StatusPill value={inquiry.status} />
            <Badge tone="violet">{titleCase(inquiry.interest)}</Badge>
          </div>
          <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
            <dt className="text-slate-500">Company</dt>
            <dd className="break-words text-slate-200">{inquiry.company || "-"}</dd>
            <dt className="text-slate-500">Email</dt>
            <dd className="break-all">
              <a href={`mailto:${inquiry.email}`} className="text-cyan-glow hover:underline">
                {inquiry.email}
              </a>
            </dd>
            <dt className="text-slate-500">Phone</dt>
            <dd className="break-words text-slate-200">{inquiry.phone || "-"}</dd>
            <dt className="text-slate-500">Received</dt>
            <dd className="text-slate-200">{formatDateTime(inquiry.created_at)}</dd>
          </dl>
        </section>

        <section aria-label="Message">
          <h3 className="mb-2 text-sm font-semibold uppercase tracking-wider text-slate-400">Message</h3>
          <p className="whitespace-pre-wrap break-words rounded-xl border border-white/10 bg-white/[0.03] p-4 text-sm text-slate-200">
            {inquiry.message}
          </p>
        </section>

        <section aria-label="Follow-up">
          <h3 className="mb-2 text-sm font-semibold uppercase tracking-wider text-slate-400">Follow-up</h3>
          <label htmlFor="inquiry-status" className="mb-1.5 block text-sm font-medium text-slate-300">
            Status
          </label>
          <select
            id="inquiry-status"
            className="field"
            value={status}
            disabled={!isMember}
            onChange={(event) => setStatus(event.target.value as InquiryStatus)}
          >
            {FILTERS.filter((item) => item.value !== "all").map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <label htmlFor="inquiry-notes" className="mb-1.5 mt-4 block text-sm font-medium text-slate-300">
            Notes
          </label>
          <textarea
            id="inquiry-notes"
            rows={4}
            maxLength={4000}
            className="field"
            value={notes}
            disabled={!isMember}
            onChange={(event) => setNotes(event.target.value)}
          />
          {isMember && (
            <button type="button" className="btn-primary btn-sm mt-3" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
              Save
            </button>
          )}
        </section>
      </div>
    </Drawer>
  );
}

export default function Inquiries() {
  const [filter, setFilter] = useState<Filter>("all");
  const [page, setPage] = useState(1);
  const [openId, setOpenId] = useState<number | null>(null);
  useEffect(() => setPage(1), [filter]);

  const { data, error, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["inquiries", filter, page],
    queryFn: () =>
      api.get<Page<Inquiry>>(`/inquiries${qs({ status: filter === "all" ? undefined : filter, page, page_size: PAGE_SIZE })}`),
    refetchInterval: 30_000,
    placeholderData: (previous) => previous,
  });
  const open = data?.items.find((item) => item.id === openId) ?? null;

  return (
    <>
      <PageHeader title="Inquiries" />
      <div role="tablist" aria-label="Filter by status" className="-mx-4 mb-5 flex gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:px-0">
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
      {data && data.items.length === 0 && <Empty>No inquiries here yet.</Empty>}

      {data && data.items.length > 0 && (
        <div className={isFetching ? "opacity-70 transition-opacity" : ""}>
          <div className="glass hidden overflow-x-auto rounded-2xl md:block">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Website inquiries</caption>
              <thead className="border-b border-white/10 text-xs uppercase tracking-wider text-slate-400">
                <tr>
                  <th scope="col" className="px-4 py-3">Name</th>
                  <th scope="col" className="px-4 py-3">Company</th>
                  <th scope="col" className="px-4 py-3">Email</th>
                  <th scope="col" className="px-4 py-3">Phone</th>
                  <th scope="col" className="px-4 py-3">Interest</th>
                  <th scope="col" className="px-4 py-3">Message</th>
                  <th scope="col" className="px-4 py-3">Date</th>
                  <th scope="col" className="px-4 py-3">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={item.id} className="border-b border-white/5 last:border-0 hover:bg-white/[0.03]">
                    <td className="px-4 py-3">
                      <button type="button" onClick={() => setOpenId(item.id)} className="text-left font-medium text-white hover:text-cyan-glow">
                        {item.name}
                      </button>
                    </td>
                    <td className="px-4 py-3 text-slate-300">{item.company || "-"}</td>
                    <td className="max-w-[12rem] break-all px-4 py-3 text-slate-300">{item.email}</td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-400">{item.phone || "-"}</td>
                    <td className="px-4 py-3 text-slate-400">{titleCase(item.interest)}</td>
                    <td className="max-w-[16rem] px-4 py-3 text-slate-400">{snippet(item.message)}</td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-400">{formatDateTime(item.created_at)}</td>
                    <td className="px-4 py-3"><StatusPill value={item.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <ul className="space-y-3 md:hidden">
            {data.items.map((item) => (
              <li key={item.id}>
                <button type="button" onClick={() => setOpenId(item.id)} className="glass w-full rounded-2xl p-4 text-left">
                  <span className="flex items-start justify-between gap-3">
                    <span className="min-w-0">
                      <span className="block font-semibold text-white">{item.name}</span>
                      <span className="block truncate text-sm text-slate-400">{item.company || item.email}</span>
                    </span>
                    <StatusPill value={item.status} />
                  </span>
                  <span className="mt-2 block text-sm text-slate-300">{snippet(item.message)}</span>
                  <span className="mt-2 block text-xs text-slate-500">
                    {titleCase(item.interest)} · {formatDateTime(item.created_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {data && <Pagination page={data.page} pageSize={data.page_size} total={data.total} onChange={setPage} />}
      {open && <InquiryDrawer inquiry={open} onClose={() => setOpenId(null)} />}
    </>
  );
}
