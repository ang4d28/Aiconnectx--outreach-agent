import { useQuery } from "@tanstack/react-query";
import { FileText, MessageSquare, Send, Sparkles, Users } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Suspense, lazy } from "react";
import { api } from "./api";
import type { Overview as OverviewData } from "./api";
import { Card, CardsSkeleton, ChartSkeleton, ErrorNote, PageHeader } from "./ui";

// recharts is a large library. The chart code is its own chunk, requested as soon as this page loads so it
// downloads while the numbers are fetched; the stat cards never wait for it.
const loadCharts = () => import("./OverviewCharts");
const OverviewCharts = lazy(loadCharts);
void loadCharts().catch(() => undefined);

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

function ChartsPlaceholder() {
  return (
    <div className="mt-6 grid gap-4 lg:grid-cols-5">
      <Card className="lg:col-span-3">
        <h2 className="mb-4 text-lg font-semibold text-white">Activity, last 14 days</h2>
        <ChartSkeleton />
      </Card>
      <Card className="lg:col-span-2">
        <h2 className="mb-4 text-lg font-semibold text-white">Funnel</h2>
        <ChartSkeleton />
      </Card>
    </div>
  );
}

export default function Overview() {
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["overview"],
    queryFn: () => api.get<OverviewData>("/dashboard/overview"),
    refetchInterval: 30_000,
  });

  const capText = data
    ? Object.entries(data.caps.limits)
        .map(([channel, limit]) => `${channel === "linkedin" ? "LinkedIn" : "Email"} ${data.caps.sent[channel] ?? 0}/${limit}`)
        .join(", ")
    : "";

  return (
    <>
      <PageHeader title="Overview" />
      {isLoading ? (
        <>
          <CardsSkeleton />
          <ChartsPlaceholder />
        </>
      ) : error || !data ? (
        <ErrorNote error={error} retry={() => refetch()} />
      ) : (
        <>
          <section aria-label="Today" className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-5">
            <StatCard label="Leads today" value={data.cards.leads_today} icon={Users} />
            <StatCard label="Drafts pending" value={data.cards.drafts_pending} icon={FileText} hint="Awaiting your approval" />
            <StatCard label="Sent today" value={data.cards.sent_today} icon={Send} hint={`Caps used: ${capText}`} />
            <StatCard label="Replies today" value={data.cards.replies_today} icon={MessageSquare} />
            <StatCard label="Interested" value={data.cards.interested_today} icon={Sparkles} />
          </section>
          <Suspense fallback={<ChartsPlaceholder />}>
            <OverviewCharts activity={data.activity} funnel={data.funnel} />
          </Suspense>
        </>
      )}
    </>
  );
}
