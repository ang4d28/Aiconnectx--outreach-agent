import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import LeadDrawer from "./LeadDrawer";
import { api, qs } from "./api";
import type { Page, ReplyRow } from "./api";
import { ChannelBadge, Empty, ErrorNote, PageHeader, Pagination, Spinner, StatusBadge, formatDateTime } from "./ui";

const PAGE_SIZE = 20;

export default function Replies() {
  const [page, setPage] = useState(1);
  const [openId, setOpenId] = useState<number | null>(null);

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["replies", page],
    queryFn: () => api.get<Page<ReplyRow>>(`/dashboard/replies${qs({ page, page_size: PAGE_SIZE })}`),
    refetchInterval: 30_000,
    placeholderData: (previous) => previous,
  });

  return (
    <>
      <PageHeader title="Replies" />
      <p className="mb-5 text-sm text-slate-400">Latest reply from each lead. Open one to see the whole thread.</p>
      {isLoading && <Spinner />}
      {error && <ErrorNote error={error} retry={() => refetch()} />}
      {data && data.items.length === 0 && <Empty>No replies yet.</Empty>}
      {data && (
        <ul className="space-y-3">
          {data.items.map((reply) => (
            <li key={reply.message_id}>
              <button
                type="button"
                onClick={() => setOpenId(reply.outreach_id)}
                className="glass w-full rounded-2xl p-4 text-left transition hover:border-cyan-glow/40 sm:p-5"
              >
                <span className="flex flex-wrap items-center justify-between gap-2">
                  <span>
                    <span className="font-semibold text-white">{reply.person}</span>
                    <span className="ml-2 text-sm text-slate-400">{reply.company ?? ""}</span>
                  </span>
                  <span className="flex items-center gap-2">
                    <StatusBadge value={reply.intent} />
                    <ChannelBadge channel={reply.channel} />
                  </span>
                </span>
                <span className="mt-2 line-clamp-3 block whitespace-pre-wrap text-sm text-slate-300">{reply.text}</span>
                <span className="mt-2 block text-xs text-slate-500">{formatDateTime(reply.received_at)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {data && <Pagination page={data.page} pageSize={data.page_size} total={data.total} onChange={setPage} />}
      {openId !== null && <LeadDrawer outreachId={openId} onClose={() => setOpenId(null)} />}
    </>
  );
}
