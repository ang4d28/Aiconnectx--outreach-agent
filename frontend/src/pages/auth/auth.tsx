import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { HttpError, LOGOUT_EVENT, api } from "../dashboard/api";
import { Spinner } from "../dashboard/ui";
import WaitingApproval from "./WaitingApproval";

export type Role = "admin" | "member" | "viewer" | "pending" | "disabled";
export type Provider = "password" | "google" | "microsoft";

export interface AuthUser {
  id: number;
  email: string;
  name: string;
  provider: Provider;
  role: Role;
  created_at: string | null;
  last_login_at: string | null;
}

interface AuthState {
  user: AuthUser | null;
  loading: boolean;
  /** viewer is read-only; member also edits and approves drafts and resolves attention items. */
  isMember: boolean;
  isAdmin: boolean;
  setUser: (user: AuthUser | null) => void;
  refresh: () => Promise<AuthUser | null>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function isActive(user: AuthUser | null): user is AuthUser {
  return user !== null && user.role !== "pending" && user.role !== "disabled";
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const me = await api.get<AuthUser>("/auth/me");
      setUser(me);
      return me;
    } catch (error) {
      // 401 is the normal "not signed in"; an unreachable server also lands on the login page.
      if (!(error instanceof HttpError) || error.status !== 401) console.warn("Could not load the session", error);
      setUser(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const onLogout = () => setUser(null);
    window.addEventListener(LOGOUT_EVENT, onLogout);
    return () => window.removeEventListener(LOGOUT_EVENT, onLogout);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } catch {
      /* the cookie may already be gone; leave the page either way */
    }
    setUser(null);
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      isMember: user?.role === "member" || user?.role === "admin",
      isAdmin: user?.role === "admin",
      setUser,
      refresh,
      logout,
    }),
    [user, loading, refresh, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}

/** Route guard: signed out goes to /login, pending and disabled accounts see the waiting page. */
export function RequireAuth() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <Spinner label="Checking your session" />;
  // Keep the query string: the backend reports a failed Google/Microsoft sign-in as ?auth_error=...
  if (!user) return <Navigate to={`/login${location.search}`} replace />;
  if (!isActive(user)) return <WaitingApproval />;
  return <Outlet />;
}

/** Inside RequireAuth: pages only admins may open. */
export function RequireAdmin() {
  const { isAdmin } = useAuth();
  return isAdmin ? <Outlet /> : <Navigate to="/dashboard" replace />;
}
