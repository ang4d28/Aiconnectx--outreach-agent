import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAdmin, RequireAuth } from "../auth/auth";
import Layout from "./Layout";
import { Pages, loaders } from "./pages";

// On a direct visit to /dashboard the Overview chunk downloads while the session check runs.
if (typeof window !== "undefined" && window.location.pathname.replace(/\/+$/, "") === "/dashboard") {
  void loaders.Overview().catch(() => undefined);
}

export default function DashboardApp() {
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

  // The query client and the "another person signed in" cache reset live in AuthLayout/AuthProvider.
  return (
    <Routes>
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route index element={<Pages.Overview />} />
          <Route path="leads" element={<Pages.Leads />} />
          <Route path="drafts" element={<Pages.Drafts />} />
          <Route path="replies" element={<Pages.Replies />} />
          <Route path="attention" element={<Pages.Attention />} />
          <Route path="inquiries" element={<Pages.Inquiries />} />
          <Route element={<RequireAdmin />}>
            <Route path="settings" element={<Pages.Settings />} />
            <Route path="users" element={<Pages.Users />} />
          </Route>
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}
