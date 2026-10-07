import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Overview as OverviewData } from "./api";
import { Card } from "./ui";

// This file is the only importer of recharts, so the library is its own chunk, loaded by Overview on demand.

const AXIS = { stroke: "#64748b", fontSize: 12 } as const;
const TOOLTIP_STYLE = {
  background: "#0D1428",
  border: "1px solid rgba(255,255,255,0.12)",
  borderRadius: 12,
  color: "#e2e8f0",
} as const;

function shortDate(value: string): string {
  return new Date(`${value}T00:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export default function OverviewCharts({ activity, funnel }: Pick<OverviewData, "activity" | "funnel">) {
  return (
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
  );
}
