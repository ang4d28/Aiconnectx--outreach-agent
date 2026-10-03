import { useQuery } from "@tanstack/react-query";
import { FileText, MessageSquare, Send, Sparkles, Users } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "./api";
import type { Overview as OverviewData } from "./api";
import { Card, ErrorNote, PageHeader, Spinner } from "./ui";

const AXIS = { stroke: "#64748b", fontSize: 12 } as const;
const TOOLTIP_STYLE = {
  background: "#0D1428",
  border: "1px solid rgba(255,255,255,0.12)",
  borderRadius: 12,
  color: "#e2e8f0",
} as const;

interface StatCardProps {
  label: string;
  value: number;
  icon: LucideIcon;
  hint?: string;
}

function StatCard({ label, value, icon: Icon, hint }: StatCardProps) {
  return (
    <Card>
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-slate-400">{label}</p>
        <Icon aria-hidden="true" className="h-5 w-5 text-cyan-glow" />
      </div>
      <p className="gradient-text mt-3 text-3xl font-extrabold">{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </Card>
  );
}

function shortDate(value: string): string {
  return new Date(`${value}T00:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export default function Overview() {
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["overview"],
    queryFn: () => api.get<OverviewData>("/dashboard/overview"),
    refetchInterval: 30_000,
  });

  if (isLoading) return <Spinner />;
  if (error || !data) return <ErrorNote error={error} retry={() => refetch()} />;

  const { cards, activity, funnel, caps } = data;
  const capText = Object.entries(caps.limits)
    .map(([channel, limit]) => `${channel === "linkedin" ? "LinkedIn" : "Email"} ${caps.sent[channel] ?? 0}/${limit}`)
    .join(", ");

  return (
    <>
      <PageHeader title="Overview" />
      <section aria-label="Today" className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-5">
        <StatCard label="Leads today" value={cards.leads_today} icon={Users} />
        <StatCard label="Drafts pending" value={cards.drafts_pending} icon={FileText} hint="Awaiting your approval" />
        <StatCard label="Sent today" value={cards.sent_today} icon={Send} hint={`Caps used: ${capText}`} />
        <StatCard label="Replies today" value={cards.replies_today} icon={MessageSquare} />
        <StatCard label="Interested" value={cards.interested_today} icon={Sparkles} />
      </section>

      <div className="mt-6 grid gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <h2 className="mb-4 text-lg font-semibold text-white">Activity, last 14 days</h2>
          <div className="h-64 sm:h-72" role="img" aria-label="Line chart of leads, messages sent and replies per day">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={activity} margin={{ top: 5, right: 8, left: -20, bottom: 0 }}>
                <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                <XAxis dataKey="date" tickFormatter={shortDate} {...AXIS} minTickGap={24} />
                <YAxis allowDecimals={false} {...AXIS} />
                <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={(label) => shortDate(String(label))} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Line type="monotone" dataKey="leads" name="Leads" stroke="#22D3EE" strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="sent" name="Sent" stroke="#3B82F6" strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="replies" name="Replies" stroke="#8B5CF6" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card className="lg:col-span-2">
          <h2 className="mb-4 text-lg font-semibold text-white">Funnel</h2>
          <div className="h-64 sm:h-72" role="img" aria-label="Funnel from leads to interested">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={funnel} layout="vertical" margin={{ top: 5, right: 24, left: 8, bottom: 0 }}>
                <CartesianGrid stroke="rgba(255,255,255,0.06)" horizontal={false} />
                <XAxis type="number" allowDecimals={false} {...AXIS} />
                <YAxis type="category" dataKey="stage" width={72} {...AXIS} />
                <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: "rgba(255,255,255,0.04)" }} />
                <Bar dataKey="count" name="Leads" fill="#22D3EE" radius={[0, 6, 6, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>
    </>
  );
}
