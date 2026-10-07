import { QueryClient } from "@tanstack/react-query";
import { HttpError } from "./api";

/** Cached data stays fresh for 30 s: moving between tabs or remounting a page does not refetch it. */
export const STALE_MS = 30_000;

/**
 * One client for the whole signed-in area. It lives above AuthProvider so the auth state can reset the cache
 * (see AuthProvider.setUser) before any page starts a query, not from an effect that runs after they have.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: STALE_MS,
      retry: (count, error) => !(error instanceof HttpError && error.status >= 400 && error.status < 500) && count < 2,
    },
  },
});
