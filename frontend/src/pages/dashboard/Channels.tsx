import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Linkedin, Loader2, LogIn, Mail, MessageCircle, RefreshCw } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { HttpError, api } from "./api";
import type { ChannelState, ChannelStatus, ChannelsPayload } from "./api";
import { Badge, Card, ErrorNote, Spinner, formatDateTime, toast } from "./ui";
import type { Tone } from "./ui";

const ICONS: Record<ChannelStatus["channel"], LucideIcon> = {
  linkedin: Linkedin,
  outlook: Mail,
  whatsapp: MessageCircle,
};

const STATES: Record<ChannelState, { label: string; tone: Tone }> = {
  connected: { label: "Connected", tone: "green" },
  needs_login: { label: "Needs login", tone: "red" },
  disabled: { label: "Disabled", tone: "slate" },
  error: { label: "Error", tone: "amber" },
  unknown: { label: "Not checked", tone: "slate" },
};

type Note = { text: string; tone: "info" | "warn" };

function useChannels() {
  return useQuery({
    queryKey: ["channels"],
    queryFn: () => api.get<ChannelsPayload>("/channels/status"),
    // Poll quickly while a LinkedIn login window is open, so the card flips to Connected by itself.
    refetchInterval: (query) => (query.state.data?.linkedin_login_running ? 3_000 : 60_000),
  });
}

function ChannelCard({ item, payload }: { item: ChannelStatus; payload: ChannelsPayload }) {
  const client = useQueryClient();
  const [note, setNote] = useState<Note | null>(null);
  const Icon = ICONS[item.channel];
  const state = STATES[item.state] ?? STATES.unknown;
  const isLinkedIn = item.channel === "linkedin";
  const loginRunning = isLinkedIn && payload.linkedin_login_running;

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["channels"] });
    client.invalidateQueries({ queryKey: ["status"] });
  };

  const check = useMutation({
    mutationFn: () => api.post<ChannelStatus>(`/channels/${item.channel}/check`),
    onSuccess: (row) => {
      client.setQueryData<ChannelsPayload>(["channels"], (current) =>
        current ? { ...current, channels: current.channels.map((c) => (c.channel === row.channel ? row : c)) } : current,
      );
      refresh();
      toast(`${row.label}: ${STATES[row.state]?.label ?? row.state}`, row.state === "connected" ? "ok" : "error");
    },
    onError: (error) => toast(error instanceof Error ? error.message : "The check failed", "error"),
  });

  const connect = useMutation({
    mutationFn: () => api.post<{ started: boolean; message: string }>("/channels/linkedin/connect"),
    onSuccess: (body) => {
      setNote({ text: body.message, tone: "info" });
      refresh();
    },
    onError: (error) =>
      setNote({ text: error instanceof HttpError ? error.message : "Could not start the LinkedIn login", tone: "warn" }),
  });

  const busy = check.isPending || loginRunning;

  return (
    <Card className="flex flex-col">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-white/10 bg-white/5">
            <Icon aria-hidden="true" className="h-5 w-5 text-cyan-200" />
          </span>
          <h3 className="truncate text-base font-semibold text-white">{item.label}</h3>
        </div>
        <Badge tone={state.tone}>{state.label}</Badge>
      </div>

      <dl className="mt-4 space-y-1.5 text-sm">
        {item.account_name && (
          <div className="flex flex-wrap gap-x-1.5 text-slate-300">
            <dt>{item.state === "connected" ? "Connected as" : "Last account"}</dt>
            <dd className="min-w-0 font-medium text-white [overflow-wrap:anywhere]">
              {item.profile_url ? (
                <a href={item.profile_url} target="_blank" rel="noopener noreferrer" className="underline-offset-2 hover:underline">
                  {item.account_name}
                </a>
              ) : (
                item.account_name
              )}
            </dd>
          </div>
        )}
        <div className="flex flex-wrap gap-x-1.5 text-slate-400">
          <dt>Last checked</dt>
          <dd>{item.last_checked_at ? formatDateTime(item.last_checked_at) : "never"}</dd>
        </div>
        {item.detail && item.state !== "connected" && (
          <div className="text-slate-400">
            <dt className="sr-only">Details</dt>
            <dd className="[overflow-wrap:anywhere]">{item.detail}</dd>
          </div>
        )}
      </dl>

      {isLinkedIn && (
        <div className="mt-4 rounded-xl border border-white/10 bg-white/[0.03] p-3 text-sm text-slate-300">
          <p>
            Connect LinkedIn opens a browser window on the machine the backend runs on. Sign in there and solve any
            check by hand; the window closes by itself once the feed loads (5 minutes max). Your password stays in
            LinkedIn&apos;s page and is never stored here.
          </p>
          <p className="mt-2">
            On a server without a screen, run{" "}
            <code className="rounded bg-navy-900/80 px-1.5 py-0.5 font-mono text-xs text-cyan-200 [overflow-wrap:anywhere]">
              {payload.login_command}
            </code>{" "}
            in a session with a display, then press Check now.
          </p>
        </div>
      )}

      {loginRunning && (
        <p role="status" className="mt-3 flex items-center gap-2 text-sm text-cyan-200">
          <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none" />
          Waiting for you to sign in in the browser window...
        </p>
      )}
      {note && !loginRunning && (
        <p
          role={note.tone === "warn" ? "alert" : "status"}
          className={`mt-3 rounded-xl border p-3 text-sm ${
            note.tone === "warn" ? "border-amber-400/30 bg-amber-400/10 text-amber-200" : "border-cyan-glow/30 bg-cyan-glow/10 text-cyan-100"
          }`}
        >
          {note.text}
        </p>
      )}

      <div className="mt-auto flex flex-wrap gap-2 pt-4">
        <button type="button" className="btn-ghost btn-sm" disabled={busy} onClick={() => check.mutate()}>
          <RefreshCw aria-hidden="true" className={`h-4 w-4 ${check.isPending ? "animate-spin motion-reduce:animate-none" : ""}`} />
          {check.isPending ? "Checking..." : "Check now"}
        </button>
        {isLinkedIn && (
          <button
            type="button"
            className="btn-primary btn-sm"
            disabled={busy || connect.isPending || item.state === "disabled"}
            onClick={() => {
              setNote(null);
              connect.mutate();
            }}
          >
            <LogIn aria-hidden="true" className="h-4 w-4" />
            Connect LinkedIn
          </button>
        )}
      </div>
    </Card>
  );
}

export default function ChannelsSection() {
  const client = useQueryClient();
  const { data, error, isLoading, refetch } = useChannels();

  // When a dashboard login window closes, refresh the header banner too.
  const wasRunning = useRef(false);
  const running = data?.linkedin_login_running ?? false;
  useEffect(() => {
    if (wasRunning.current && !running) client.invalidateQueries({ queryKey: ["status"] });
    wasRunning.current = running;
  }, [client, running]);

  return (
    <section id="channels" aria-labelledby="channels-heading" className="scroll-mt-24">
      <h2 id="channels-heading" className="text-lg font-semibold text-white">
        Channels
      </h2>
      <p className="mb-4 mt-1 text-sm text-slate-400">
        Connection status of each channel. LinkedIn is checked every 6 hours and before each LinkedIn batch.
      </p>
      {isLoading ? (
        <Spinner label="Loading channels" />
      ) : !data ? (
        <ErrorNote error={error} retry={() => refetch()} />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {data.channels.map((item) => (
            <ChannelCard key={item.channel} item={item} payload={data} />
          ))}
        </div>
      )}
    </section>
  );
}
