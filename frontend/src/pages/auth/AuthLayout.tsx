import { QueryClientProvider } from "@tanstack/react-query";
import { Outlet } from "react-router-dom";
import { queryClient } from "../dashboard/queryClient";
import { AuthProvider } from "./auth";

/**
 * Wraps the sign-in, waiting and dashboard routes. The landing page never loads or calls this.
 * The query client sits above AuthProvider so signing in or out resets the cache before pages mount.
 */
export default function AuthLayout() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Outlet />
      </AuthProvider>
    </QueryClientProvider>
  );
}
