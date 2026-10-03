import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { api } from "./api";
import type { LeadDetail } from "./api";
import { Badge, ChannelBadge, Drawer, ErrorNote, Spinner, StatusBadge, formatDateTime } from "./ui";

interface Props {
  outreachId: number;
  onClose: () => void;
}

export default function LeadDrawer({ outreachId, onClose }: Props) {
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["lead", outreachId],
    queryFn: () => api.get<LeadDetail>(`/dashboard/leads/${outreachId}`),
  });

  return (
    <Drawer title={data ? data.person : "Lead"} onClose={onClose}>
      {isLoading && <Spinner />}
      {error && <ErrorNote error={error} retry={() => refetch()} />}
      {data && (
        <div className="space-y-6">
          <section aria-label="Contact">
            <p className="text-slate-300">
              {data.title ? `${data.title}, ` : ""}
              {data.company ?? "Unknown company"}
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <ChannelBadge channel={data.channel} />
              <StatusBadge value={data.status} />
              <Badge>{data.source === "post" ? "From a post" : "Company search"}</Badge>
            </div>
            <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
              {data.email && (
                <>
                  <dt className="text-slate-500">Email</dt>
                  <dd className="break-all text-slate-200">{data.email}</dd>
                </>
              )}
              {data.linkedin_url && (
                <>
                  <dt className="text-slate-500">LinkedIn</dt>
                  <dd className="break-all">
                    <a href={data.linkedin_url} target="_blank" rel="noopener noreferrer" className="text-cyan-glow hover:underline">
                      Profile <ExternalLink aria-hidden="true" className="inline h-3.5 w-3.5" />
                    </a>
                  </dd>
                </>
              )}
              <dt className="text-slate-500">First message</dt>
              <dd className="text-slate-200">{formatDateTime(data.sent_at)}</dd>
              <dt className="text-slate-500">Next follow-up</dt>
              <dd className="text-slate-200">
                {formatDateTime(data.follow_up_due)} ({data.follow_up_count} sent)
              </dd>
            </dl>
          </section>

          {data.post && (
            <section aria-label="Post">
              <h3 className="mb-2 text-sm font-semibold uppercase tracking-wider text-slate-400">Hiring post</h3>
              <div className="rounded-xl border border-white/10 bg-white/[0.03] p-4 text-sm">
                {data.post.roles.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1.5">
                    {data.post.roles.map((role) => (
                      <Badge key={role} tone="cyan">
                        {role}
                      </Badge>
                    ))}
                  </div>
                )}
                <p className="whitespace-pre-wrap text-slate-300">{data.post.text || "No text saved."}</p>
                <a
                  href={data.post.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-3 inline-flex items-center gap-1 text-cyan-glow hover:underline"
                >
                  Open post <ExternalLink aria-hidden="true" className="h-3.5 w-3.5" />
                </a>
              </div>
            </section>
          )}

          <section aria-label="Thread">
            <h3 className="mb-2 text-sm font-semibold uppercase tracking-wider text-slate-400">Thread</h3>
            {data.messages.length === 0 ? (
              <p className="text-sm text-slate-500">No messages yet.</p>
            ) : (
              <ol className="space-y-3">
                {data.messages.map((message) => {
                  const inbound = message.direction === "inbound";
                  return (
                    <li key={message.id} className={`flex ${inbound ? "justify-start" : "justify-end"}`}>
                      <div
                        className={`max-w-[85%] rounded-2xl px-4 py-3 text-sm ${
                          inbound ? "rounded-bl-md bg-white/10 text-slate-100" : "rounded-br-md bg-electric/25 text-slate-100"
                        }`}
                      >
                        <p className="whitespace-pre-wrap">{message.text}</p>
                        <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                          {inbound ? "Reply" : "Us"} · {formatDateTime(message.at)}
                          {!inbound && <StatusBadge value={message.status} />}
                        </p>
                      </div>
                    </li>
                  );
                })}
              </ol>
            )}
          </section>
        </div>
      )}
    </Drawer>
  );
}
