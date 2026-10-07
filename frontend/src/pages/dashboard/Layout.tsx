import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  FileText,
  Inbox,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  Settings as SettingsIcon,
  UserCog,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Suspense, useEffect } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import Logo from "../../components/Logo";
import { useAuth } from "../auth/auth";
import { HttpError, api } from "./api";
import type { Badges, DashboardStatus } from "./api";
import { preloadPages } from "./pages";
import { Badge, ListSkeleton, Toaster, titleCase, toast } from "./ui";

interface NavItem {
  to: string;
  label: string;
  short: string;
  icon: LucideIcon;
  end?: boolean;
  adminOnly?: boolean;
}

const NAV: NavItem[] = [
  { to: "/dashboard", label: "Overview", short: "Overview", icon: LayoutDashboard, end: true },
  { to: "/dashboard/leads", label: "Leads", short: "Leads", icon: Users },
  { to: "/dashboard/drafts", label: "Drafts", short: "Drafts", icon: FileText },
  { to: "/dashboard/replies", label: "Replies", short: "Replies", icon: MessageSquare },
  { to: "/dashboard/attention", label: "Needs attention", short: "Attention", icon: AlertTriangle },
  { to: "/dashboard/inquiries", label: "Inquiries", short: "Inquiries", icon: Inbox },
  { to: "/dashboard/settings", label: "Settings", short: "Settings", icon: SettingsIcon, adminOnly: true },
  { to: "/dashboard/users", label: "Users", short: "Users", icon: UserCog, adminOnly: true },
];

function useStatus() {
  return useQuery({
    queryKey: ["status"],
    queryFn: () => api.get<DashboardStatus>("/dashboard/status"),
    refetchInterval: 15_000,
  });
}

/** The sidebar counts (attention, inquiries, pending accounts) from one lightweight endpoint. */
function useBadges() {
  return useQuery({
    queryKey: ["badges"],
    queryFn: () => api.get<Badges>("/dashboard/badges"),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}

function PauseSwitch({ status }: { status: DashboardStatus | undefined }) {
  const client = useQueryClient();
  const { isAdmin } = useAuth();
  const mutation = useMutation({
    mutationFn: (paused: boolean) => api.post<{ paused: boolean }>("/dashboard/pause", { paused }),
    onMutate: async (paused) => {
      await client.cancelQueries({ queryKey: ["status"] });
      const previous = client.getQueryData<DashboardStatus>(["status"]);
      if (previous) client.setQueryData(["status"], { ...previous, paused });
      return { previous };
    },
    onError: (error, _paused, context) => {
      if (context?.previous) client.setQueryData(["status"], context.previous);
      toast(error instanceof HttpError ? error.message : "Could not change the pause setting", "error");
    },
    onSettled: () => client.invalidateQueries({ queryKey: ["status"] }),
  });
  const paused = status?.paused ?? false;
  if (!isAdmin) {
    // Only admins may pause; everyone else just sees the state.
    if (!status) return null;
    return <Badge tone={paused ? "amber" : "slate"}>{paused ? "Paused" : "Running"}</Badge>;
  }
  return (
    <button
      type="button"
      role="switch"
      aria-checked={paused}
      aria-label="Pause all sending"
      disabled={!status || mutation.isPending}
      onClick={() => mutation.mutate(!paused)}
      className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm font-semibold transition ${
        paused ? "border-amber-400/50 bg-amber-400/15 text-amber-200" : "border-white/15 bg-white/5 text-slate-200"
      }`}
    >
      <span aria-hidden="true" className={`relative h-5 w-9 rounded-full transition ${paused ? "bg-amber-400" : "bg-slate-600"}`}>
        <span
          className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${paused ? "left-[1.1rem]" : "left-0.5"}`}
        />
      </span>
      {paused ? "Paused" : "Pause"}
    </button>
  );
}

function Banners({ status }: { status: DashboardStatus | undefined }) {
  const client = useQueryClient();
  const { isAdmin } = useAuth();
  const enable = useMutation({
    mutationFn: (channel: string) => api.post(`/dashboard/channels/${channel}/enable`),
    onSuccess: () => {
      toast("Channel re-enabled");
      client.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : "Could not re-enable", "error"),
  });
  if (!status) return null;
  return (
    <div className="space-y-2">
      {status.linkedin_needs_login && (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 border-b border-rose-400/40 bg-rose-600/90 px-4 py-3 text-sm font-medium text-white"
        >
          <span>
            LinkedIn needs a new login, so all LinkedIn actions are stopped.{" "}
            {isAdmin ? "Reconnect it under Settings > Channels." : "An admin has to reconnect it."}
          </span>
          {isAdmin && (
            <Link
              to="/dashboard/settings#channels"
              className="rounded-lg bg-white px-3 py-1.5 font-semibold text-rose-700 hover:bg-rose-50"
            >
              Reconnect LinkedIn
            </Link>
          )}
        </div>
      )}
      {status.disabled_channels.map((item) => (
        <div
          key={item.channel}
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 border-b border-rose-400/40 bg-rose-600/90 px-4 py-3 text-sm font-medium text-white"
        >
          <span>
            {titleCase(item.channel)} is paused because of an error
            {item.reason ? `: ${item.reason}` : "."} {isAdmin ? "Fix the cause, then re-enable it." : "An admin has to fix it and re-enable it."}
          </span>
          {isAdmin && (
            <button
              type="button"
              className="rounded-lg bg-white px-3 py-1.5 font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-60"
              disabled={enable.isPending}
              onClick={() => enable.mutate(item.channel)}
            >
              Re-enable
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

export default function Layout() {
  const { user, isAdmin, logout } = useAuth();
  const { data: status } = useStatus();
  const { data: badges } = useBadges();
  const nav = NAV.filter((item) => isAdmin || !item.adminOnly);
  const badgeFor = (to: string): number =>
    to.endsWith("attention")
      ? badges?.attention ?? 0
      : to.endsWith("users")
        ? badges?.users_pending ?? 0
        : to.endsWith("inquiries")
          ? badges?.inquiries_new ?? 0
          : 0;
  const onLogout = () => void logout();

  // Once the first screen is up, fetch the other pages' code in the background so tab clicks are instant.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      preloadPages(["Leads", "Drafts", "Replies", "Attention", "Inquiries"]);
      if (isAdmin) preloadPages(["Settings", "Users"]);
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [isAdmin]);

  return (
    <div className="min-h-screen md:pl-60">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 flex-col border-r border-white/10 bg-navy-900/80 p-4 backdrop-blur-xl md:flex">
        <div className="px-2 pb-6 pt-2">
          <Logo />
        </div>
        <nav aria-label="Dashboard" className="flex flex-1 flex-col gap-1">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                  isActive ? "bg-white/10 text-white" : "text-slate-400 hover:bg-white/5 hover:text-white"
                }`
              }
            >
              <item.icon aria-hidden="true" className="h-5 w-5" />
              {item.label}
              {badgeFor(item.to) > 0 && (
                <span className="ml-auto rounded-full bg-rose-500 px-2 py-0.5 text-xs font-bold text-white">{badgeFor(item.to)}</span>
              )}
            </NavLink>
          ))}
        </nav>
      </aside>

      <header className="sticky top-0 z-20 border-b border-white/10 bg-navy-950/80 backdrop-blur-xl">
        <div className="flex h-14 items-center justify-between gap-3 px-4 sm:px-6">
          <span className="md:hidden">
            <Logo iconOnly />
          </span>
          <span className="hidden text-sm text-slate-400 md:block">Agent control</span>
          <div className="flex items-center gap-2 sm:gap-3">
            {status && (
              <span title={status.dry_run ? "Nothing is sent to leads" : "Messages are being sent"}>
                <Badge tone={status.dry_run ? "amber" : "red"}><span className="whitespace-nowrap">{status.dry_run ? "DRY RUN" : "LIVE"}</span></Badge>
              </span>
            )}
            <PauseSwitch status={status} />
            {user && (
              <span className="hidden max-w-[10rem] truncate text-sm text-slate-300 sm:block" title={user.email}>
                {user.name}
              </span>
            )}
            {user && <span className="hidden sm:inline-flex"><Badge tone={user.role === "admin" ? "violet" : user.role === "member" ? "cyan" : "slate"}>{titleCase(user.role)}</Badge></span>}
            <button
              type="button"
              onClick={onLogout}
              aria-label={user ? `Sign out ${user.name}` : "Sign out"}
              className="inline-flex h-10 items-center justify-center gap-2 rounded-lg px-2.5 text-sm font-medium text-slate-300 hover:bg-white/10 hover:text-white"
            >
              <LogOut aria-hidden="true" className="h-5 w-5" />
              <span className="hidden sm:inline">Sign out</span>
            </button>
          </div>
        </div>
        <Banners status={status} />
        {status?.paused && (
          <p role="status" className="border-t border-amber-400/30 bg-amber-400/10 px-4 py-2 text-sm text-amber-200">
            All sending is paused. Nothing goes out until you switch Pause off (or send START on WhatsApp).
          </p>
        )}
      </header>

      <main className="px-4 pb-28 pt-6 sm:px-6 md:pb-10">
        {/* The shell above is already on screen; only this area waits for a page's code. */}
        <Suspense fallback={<ListSkeleton label="Loading page" />}>
          <Outlet />
        </Suspense>
      </main>

      <nav
        aria-label="Dashboard"
        style={{ gridTemplateColumns: `repeat(${nav.length}, minmax(0, 1fr))` }}
        className="fixed inset-x-0 bottom-0 z-30 grid border-t border-white/10 bg-navy-900/95 pb-[env(safe-area-inset-bottom)] backdrop-blur-xl md:hidden"
      >
        {nav.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              `relative flex min-h-[3.5rem] flex-col items-center justify-center gap-0.5 text-[0.65rem] font-medium ${
                isActive ? "text-cyan-glow" : "text-slate-400"
              }`
            }
          >
            <item.icon aria-hidden="true" className="h-5 w-5" />
            {item.short}
            {badgeFor(item.to) > 0 && (
              <span className="absolute right-2 top-1.5 min-w-[1.1rem] rounded-full bg-rose-500 px-1 text-center text-[0.6rem] font-bold text-white">
                {badgeFor(item.to)}
              </span>
            )}
          </NavLink>
        ))}
      </nav>
      <Toaster />
    </div>
  );
}
