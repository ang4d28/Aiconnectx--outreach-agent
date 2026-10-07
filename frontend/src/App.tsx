import { Suspense, lazy } from "react";
import { BrowserRouter, Route, Routes, useLocation } from "react-router-dom";
import ShellSkeleton from "./components/ShellSkeleton";
import Landing from "./pages/Landing";

// Everything behind the member login is split out: landing visitors never download it
// (or call /api/auth/me).
const AuthLayout = lazy(() => import("./pages/auth/AuthLayout"));
const AuthPage = lazy(() => import("./pages/auth/AuthPage"));
const WaitingRoute = lazy(() => import("./pages/auth/WaitingRoute"));
const DashboardApp = lazy(() => import("./pages/dashboard/DashboardApp"));

/** While a route chunk loads: the dashboard shape under /dashboard, a plain note elsewhere. Never a blank page. */
function Fallback() {
  const { pathname } = useLocation();
  if (pathname.startsWith("/dashboard")) return <ShellSkeleton />;
  return (
    <p role="status" className="p-8 text-center text-slate-400">
      Loading...
    </p>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={<Fallback />}>
        <Routes>
          <Route element={<AuthLayout />}>
            <Route path="/login" element={<AuthPage mode="login" />} />
            <Route path="/signup" element={<AuthPage mode="signup" />} />
            <Route path="/waiting" element={<WaitingRoute />} />
            <Route path="/dashboard/*" element={<DashboardApp />} />
          </Route>
          <Route path="*" element={<Landing />} />
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
