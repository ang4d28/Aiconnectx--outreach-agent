import { Outlet } from "react-router-dom";
import { AuthProvider } from "./auth";

/** Wraps the sign-in, waiting and dashboard routes. The landing page never loads or calls this. */
export default function AuthLayout() {
  return (
    <AuthProvider>
      <Outlet />
    </AuthProvider>
  );
}
