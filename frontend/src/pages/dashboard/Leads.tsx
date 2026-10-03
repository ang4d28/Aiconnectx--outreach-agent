import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, ExternalLink, Loader2, RefreshCw, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { useAuth } from "../auth/auth";
import LeadDrawer from "./LeadDrawer";
import { api, qs } from "./api";
import type { Lead, Page, SheetInfo } from "./api";
import { ChannelBadge, Empty, ErrorNote, PageHeader, Pagination, Spinner, StatusBadge, formatDateTime, titleCase, toast } from "./ui";

const STATUSES = [
  "new", "qualified", "targeted", "enriched", "drafted", "connect_sent", "connected", "dm_sent", "contacted",
  "replied", "interested", "no_opportunity", "wrong_person", "not_now", "future_pool", "unsubscribed", "closed",
];
const PAGE_SIZE = 20;

function useDebounced(value: string, delay = 300): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

function ExportBar({ filters }: { filters: Record<string, string> }) {
  const client = useQueryClient();
  const { isMember } = useAuth();
  const { data: sheet } = useQuery({
    queryKey: ["sheet"],
    queryFn: () => api.get<SheetInfo>("/sheet"),
    refetchInterval: 30_000,
  });

  const exportFile = useMutation({
    // The download carries the filters that are on screen right now.
    mutationFn: () => api.download(`/export.xlsx${qs(filters)}`),
    onSuccess: ({ blob, filename }) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
    },
    onError: (error) => toast(error instanceof Error ? error.message : "Could not download the file", "error"),
  });
  const sync = useMutation({
    mutationFn: () => api.post<{ updated: number; added: number }>("/sheet/sync"),
    onSuccess: (result) => {
      toast(`Sheet synced: ${result.added} added, ${result.updated} updated`);
      void client.invalidateQueries({ queryKey: ["sheet"] });
    },
    onError: (error) => toast(error instanceof Error ? error.message : "Sync failed", "error"),
  });

  const filtered = Object.values(filters).some(Boolean);
  return (
    <div className="mb-5 flex flex-wrap items-center gap-x-3 gap-y-2">
      <button type="button" className="btn-primary btn-sm" disabled={exportFile.isPending} onClick={() => exportFile.mutate()}>
        {exportFile.isPending ? (
          <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none" />
        ) : (
          <Download aria-hidden="true" className="h-4 w-4" />
        )}
        Download Excel{filtered ? " (filtered)" : ""}
      </button>
      {sheet?.configured && sheet.url && (
        <>
          <a href={sheet.url} target="_blank" rel="noopener noreferrer" className="btn-ghost btn-sm">
            <ExternalLink aria-hidden="true" className="h-4 w-4" />
            Open Google Sheet
          </a>
          {isMember && (
            <button type="button" className="btn-ghost btn-sm" disabled={sync.isPending} onClick={() => sync.mutate()}>
              <RefreshCw aria-hidden="true" className={`h-4 w-4 ${sync.isPending ? "animate-spin motion-reduce:animate-none" : ""}`} />
              Sync now
            </button>
          )}
          <span className="basis-full text-xs text-slate-500 sm:basis-auto">
            {sheet.last_synced_at ? `Last synced ${formatDateTime(sheet.last_synced_at)}` : "Not synced yet"}
          </span>
        </>
      )}
    </div>
  );
}

export default function Leads() {
  const [status, setStatus] = useState("");
  const [channel, setChannel] = useState("");
  const [source, setSource] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [openId, setOpenId] = useState<number | null>(null);
  const q = useDebounced(search.trim());

  useEffect(() => setPage(1), [status, channel, source, q]);

  const { data, error, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["leads", status, channel, source, q, page],
    queryFn: () => api.get<Page<Lead>>(`/dashboard/leads${qs({ status, channel, source, q, page, page_size: PAGE_SIZE })}`),
    placeholderData: (previous) => previous,
  });

  return (
    <>
      <PageHeader title="Leads" />
      <ExportBar filters={{ status, channel, source, q }} />
      <form role="search" aria-label="Filter leads" onSubmit={(event) => event.preventDefault()} className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="relative sm:col-span-2 lg:col-span-1">
          <label htmlFor="lead-search" className="sr-only">
            Search leads
          </label>
          <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
          <input
            id="lead-search"
            type="search"
            placeholder="Search name, company, email"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            className="field pl-9"
          />
        </div>
        <div>
          <label htmlFor="lead-status" className="sr-only">
            Status
          </label>
          <select id="lead-status" value={status} onChange={(event) => setStatus(event.target.value)} className="field">
            <option value="">All statuses</option>
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {titleCase(value)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="lead-channel" className="sr-only">
            Channel
          </label>
          <select id="lead-channel" value={channel} onChange={(event) => setChannel(event.target.value)} className="field">
            <option value="">All channels</option>
            <option value="linkedin">LinkedIn</option>
            <option value="email">Email</option>
          </select>
        </div>
        <div>
          <label htmlFor="lead-source" className="sr-only">
            Source
          </label>
          <select id="lead-source" value={source} onChange={(event) => setSource(event.target.value)} className="field">
            <option value="">All sources</option>
            <option value="post">From a post</option>
            <option value="search">Company search</option>
          </select>
        </div>
      </form>

      {isLoading && <Spinner />}
      {error && <ErrorNote error={error} retry={() => refetch()} />}
      {data && data.items.length === 0 && <Empty>No leads match these filters.</Empty>}

      {data && data.items.length > 0 && (
        <div className={isFetching ? "opacity-70 transition-opacity" : ""}>
          <div className="glass hidden overflow-hidden rounded-2xl md:block">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Leads</caption>
              <thead className="border-b border-white/10 text-xs uppercase tracking-wider text-slate-400">
                <tr>
                  <th scope="col" className="px-4 py-3">Person</th>
                  <th scope="col" className="px-4 py-3">Company</th>
                  <th scope="col" className="px-4 py-3">Channel</th>
                  <th scope="col" className="px-4 py-3">Status</th>
                  <th scope="col" className="px-4 py-3">Source</th>
                  <th scope="col" className="px-4 py-3">Sent</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((lead) => (
                  <tr key={lead.id} className="border-b border-white/5 last:border-0 hover:bg-white/[0.03]">
                    <td className="px-4 py-3">
                      <button type="button" onClick={() => setOpenId(lead.id)} className="text-left font-medium text-white hover:text-cyan-glow">
                        {lead.person}
                      </button>
                      {lead.title && <p className="text-xs text-slate-500">{lead.title}</p>}
                    </td>
                    <td className="px-4 py-3 text-slate-300">{lead.company ?? "-"}</td>
                    <td className="px-4 py-3"><ChannelBadge channel={lead.channel} /></td>
                    <td className="px-4 py-3"><StatusBadge value={lead.status} /></td>
                    <td className="px-4 py-3 text-slate-400">{lead.source === "post" ? "Post" : "Search"}</td>
                    <td className="px-4 py-3 text-slate-400">{formatDateTime(lead.sent_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <ul className="space-y-3 md:hidden">
            {data.items.map((lead) => (
              <li key={lead.id}>
                <button type="button" onClick={() => setOpenId(lead.id)} className="glass w-full rounded-2xl p-4 text-left">
                  <span className="flex items-start justify-between gap-3">
                    <span>
                      <span className="block font-semibold text-white">{lead.person}</span>
                      <span className="block text-sm text-slate-400">{lead.company ?? "Unknown company"}</span>
                    </span>
                    <StatusBadge value={lead.status} />
                  </span>
                  <span className="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                    <ChannelBadge channel={lead.channel} />
                    {lead.source === "post" ? "From a post" : "Company search"} · {formatDateTime(lead.sent_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {data && <Pagination page={data.page} pageSize={data.page_size} total={data.total} onChange={setPage} />}
      {openId !== null && <LeadDrawer outreachId={openId} onClose={() => setOpenId(null)} />}
    </>
  );
}
