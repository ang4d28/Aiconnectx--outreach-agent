import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAdmin, RequireAuth, useAuth } from "../auth/auth";
import Attention from "./Attention";
import Drafts from "./Drafts";
import Inquiries from "./Inquiries";
import Layout from "./Layout";
import Leads from "./Leads";
import Overview from "./Overview";
import Replies from "./Replies";
import Settings from "./Settings";
import Users from "./Users";
import { HttpError } from "./api";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        retry: (count, error) => !(error instanceof HttpError && error.status >= 400 && error.status < 500) && count < 2,
      },
    },
  });
}

export default function DashboardApp() {
  const client = useMemo(makeClient, []);
  const { user } = useAuth();
  const userId = user?.id ?? null;

  useEffect(() => {
    document.title = "AI ConnectX Dashboard";
    const robots = document.createElement("meta");
    robots.name = "robots";
    robots.content = "noindex, nofollow";
    document.head.appendChild(robots);
    return () => {
      robots.remove();
    };
  }, []);

  // Another person signing in on this browser must never see the previous person's cached data.
  useEffect(() => {
    client.clear();
  }, [client, userId]);

  return (
    <QueryClientProvider client={client}>
      <Routes>
        <Route element={<RequireAuth />}>
          <Route element={<Layout />}>
            <Route index element={<Overview />} />
            <Route path="leads" element={<Leads />} />
            <Route path="drafts" element={<Drafts />} />
            <Route path="replies" element={<Replies />} />
            <Route path="attention" element={<Attention />} />
            <Route path="inquiries" element={<Inquiries />} />
            <Route element={<RequireAdmin />}>
              <Route path="settings" element={<Settings />} />
              <Route path="users" element={<Users />} />
            </Route>
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Route>
        </Route>
      </Routes>
    </QueryClientProvider>
  );
}
