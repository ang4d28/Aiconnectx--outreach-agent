import { Suspense, lazy } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import Landing from "./pages/Landing";

// Everything behind the member login is split out: landing visitors never download it
// (or call /api/auth/me).
const AuthLayout = lazy(() => import("./pages/auth/AuthLayout"));
const AuthPage = lazy(() => import("./pages/auth/AuthPage"));
const WaitingRoute = lazy(() => import("./pages/auth/WaitingRoute"));
const DashboardApp = lazy(() => import("./pages/dashboard/DashboardApp"));

export default function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={<p role="status" className="p-8 text-center text-slate-400">Loading...</p>}>
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
