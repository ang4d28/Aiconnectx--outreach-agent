import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import ShellSkeleton from "../../components/ShellSkeleton";
import { HttpError, LOGOUT_EVENT, api } from "../dashboard/api";
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
  /** True only until the first /api/auth/me answer. The route guard shows a skeleton meanwhile. */
  loading: boolean;
  /** viewer is read-only; member also edits and approves drafts and resolves attention items. */
  isMember: boolean;
  isAdmin: boolean;
  /** Use after a successful sign-in (or sign-out): updates the state and the cache before anything navigates. */
  setUser: (user: AuthUser | null) => void;
  refresh: () => Promise<AuthUser | null>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export const ME_KEY = ["auth", "me"] as const;

export function isActive(user: AuthUser | null): user is AuthUser {
  return user !== null && user.role !== "pending" && user.role !== "disabled";
}

async function fetchMe(): Promise<AuthUser | null> {
  try {
    return await api.get<AuthUser>("/auth/me");
  } catch (error) {
    // 401 is the normal "not signed in"; an unreachable server also lands on the login page.
    if (!(error instanceof HttpError) || error.status !== 401) console.warn("Could not load the session", error);
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const [user, setUserState] = useState<AuthUser | null>(() => client.getQueryData<AuthUser | null>(ME_KEY) ?? null);
  const [loading, setLoading] = useState(() => client.getQueryData(ME_KEY) === undefined);
  const userId = useRef<number | null>(user?.id ?? null);

  const setUser = useCallback(
    (next: AuthUser | null) => {
      const nextId = next?.id ?? null;
      if (userId.current !== nextId) {
        // Another person (or nobody) is now signed in: nothing cached for the previous user may survive.
        // Done here, synchronously and before the state changes, so it cannot orphan queries that a page
        // mounted for the new user has already started.
        client.removeQueries({ predicate: (query) => query.queryKey[0] !== ME_KEY[0] });
        userId.current = nextId;
      }
      client.setQueryData(ME_KEY, next);
      setUserState(next);
      setLoading(false);
    },
    [client],
  );

  const refresh = useCallback(async () => {
    const me = await fetchMe();
    setUser(me);
    return me;
  }, [setUser]);

  // The first answer. fetchQuery shares one in-flight request (React StrictMode mounts twice in development).
  useEffect(() => {
    let live = true;
    void client
      .fetchQuery({ queryKey: ME_KEY, queryFn: fetchMe, staleTime: Infinity, gcTime: Infinity })
      .then((me) => live && setUser(me));
    return () => {
      live = false;
    };
  }, [client, setUser]);

  useEffect(() => {
    const onLogout = () => setUser(null);
    window.addEventListener(LOGOUT_EVENT, onLogout);
    return () => window.removeEventListener(LOGOUT_EVENT, onLogout);
  }, [setUser]);

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } catch {
      /* the cookie may already be gone; leave the page either way */
    }
    setUser(null);
  }, [setUser]);

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
    [user, loading, setUser, refresh, logout],
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
  if (loading) return <ShellSkeleton label="Checking your session" />;
  // Keep the query string: the backend reports a failed Microsoft sign-in as ?auth_error=...
  if (!user) return <Navigate to={`/login${location.search}`} replace />;
  if (!isActive(user)) return <WaitingApproval />;
  return <Outlet />;
}

/** Inside RequireAuth: pages only admins may open. */
export function RequireAdmin() {
  const { isAdmin } = useAuth();
  return isAdmin ? <Outlet /> : <Navigate to="/dashboard" replace />;
}
