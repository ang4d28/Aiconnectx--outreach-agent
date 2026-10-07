import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, KeyRound, UserX } from "lucide-react";
import { useState } from "react";
import type { FormEvent } from "react";
import { useAuth } from "../auth/auth";
import type { AuthUser, Role } from "../auth/auth";
import { api } from "./api";
import { Badge, Card, Drawer, Empty, ErrorNote, ListSkeleton, PageHeader, formatDateTime, titleCase, toast } from "./ui";
import type { Tone } from "./ui";

type Grantable = "viewer" | "member" | "admin";
type Filter = "all" | "pending" | "active" | "disabled";

const GRANTABLE: Grantable[] = ["viewer", "member", "admin"];
const ROLE_HELP: Record<Grantable, string> = {
  viewer: "Viewer: read only",
  member: "Member: edits and approves drafts",
  admin: "Admin: everything, including settings and users",
};
const ROLE_TONES: Record<Role, Tone> = {
  admin: "violet",
  member: "cyan",
  viewer: "slate",
  pending: "amber",
  disabled: "red",
};
const PROVIDER_LABELS: Record<string, string> = { password: "Email", microsoft: "Microsoft" };
const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "pending", label: "Pending" },
  { value: "active", label: "Active" },
  { value: "disabled", label: "Disabled" },
];

interface UserList {
  items: AuthUser[];
  total: number;
}

export function RoleBadge({ role }: { role: Role }) {
  return <Badge tone={ROLE_TONES[role]}>{titleCase(role)}</Badge>;
}

function matches(user: AuthUser, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "active") return user.role !== "pending" && user.role !== "disabled";
  return user.role === filter;
}

function RoleSelect({
  id,
  value,
  onChange,
  disabled,
  placeholder,
}: {
  id: string;
  value: Grantable | "";
  onChange: (role: Grantable) => void;
  disabled?: boolean;
  placeholder?: string;
}) {
  return (
    <select
      id={id}
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value as Grantable)}
      className="field !w-auto !px-3 !py-2 !text-sm"
    >
      {placeholder && (
        <option value="" disabled>
          {placeholder}
        </option>
      )}
      {GRANTABLE.map((role) => (
        <option key={role} value={role}>
          {ROLE_HELP[role]}
        </option>
      ))}
    </select>
  );
}

function ResetPasswordDrawer({ user, onClose }: { user: AuthUser; onClose: () => void }) {
  const [password, setPassword] = useState("");
  const [temporary, setTemporary] = useState<string | null>(null);
  const [error, setError] = useState("");
  const reset = useMutation({
    mutationFn: () =>
      api.post<{ temporary_password: string | null }>(`/admin/users/${user.id}/reset-password`, password ? { password } : {}),
    onSuccess: (result) => {
      setError("");
      if (result.temporary_password) setTemporary(result.temporary_password);
      else {
        toast(`Password changed for ${user.name}`);
        onClose();
      }
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Could not reset the password"),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (password && password.length < 10) {
      setError("Use at least 10 characters, or leave it empty to generate one.");
      return;
    }
    reset.mutate();
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(temporary ?? "");
      toast("Copied");
    } catch {
      toast("Could not copy. Select the password and copy it by hand.", "error");
    }
  };

  return (
    <Drawer title={`Reset password: ${user.name}`} onClose={onClose}>
      {temporary ? (
        <div>
          <p className="text-sm text-slate-300">
            Give this temporary password to {user.name} through a private channel. It is shown only once, and every session they
            had is now signed out.
          </p>
          <p className="mt-4 select-all break-all rounded-xl border border-cyan-glow/30 bg-cyan-glow/10 p-4 font-mono text-base text-white">
            {temporary}
          </p>
          <div className="mt-4 flex gap-2">
            <button type="button" className="btn-primary btn-sm" onClick={copy}>
              <Copy aria-hidden="true" className="h-4 w-4" />
              Copy
            </button>
            <button type="button" className="btn-ghost btn-sm" onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} noValidate>
          <p className="text-sm text-slate-300">
            Set a new password for <strong className="text-white">{user.email}</strong>. They are signed out everywhere.
          </p>
          <label htmlFor="reset-password" className="mb-1.5 mt-5 block text-sm font-medium text-slate-200">
            New password (optional)
          </label>
          <input
            id="reset-password"
            type="text"
            autoComplete="off"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="Leave empty to generate one"
            className="field"
          />
          <p role="alert" className="mt-2 min-h-[1.25rem] text-sm text-rose-300">
            {error}
          </p>
          <button type="submit" className="btn-primary btn-sm mt-2" disabled={reset.isPending}>
            <KeyRound aria-hidden="true" className="h-4 w-4" />
            Reset password
          </button>
        </form>
      )}
    </Drawer>
  );
}

function Actions({ user, onResetPassword }: { user: AuthUser; onResetPassword: (user: AuthUser) => void }) {
  const client = useQueryClient();
  const [approveAs, setApproveAs] = useState<Grantable>("viewer");
  const done = (message: string) => {
    toast(message);
    void client.invalidateQueries({ queryKey: ["users"] });
    void client.invalidateQueries({ queryKey: ["badges"] });
  };
  const fail = (error: unknown) => toast(error instanceof Error ? error.message : "Action failed", "error");

  const approve = useMutation({
    mutationFn: (role: Grantable) => api.post(`/admin/users/${user.id}/approve`, { role }),
    onSuccess: () => done(`${user.name} approved as ${approveAs}`),
    onError: fail,
  });
  const changeRole = useMutation({
    mutationFn: (role: Grantable) => api.patch(`/admin/users/${user.id}/role`, { role }),
    onSuccess: (_result, role) => done(`${user.name} is now ${role}`),
    onError: fail,
  });
  const disable = useMutation({
    mutationFn: () => api.post(`/admin/users/${user.id}/disable`),
    onSuccess: () => done(`${user.name} disabled`),
    onError: fail,
  });
  const busy = approve.isPending || changeRole.isPending || disable.isPending;

  const pickRole = (role: Grantable) => {
    if (role === "admin" && !window.confirm(`Make ${user.name} an admin? Admins can change settings, go live and manage users.`)) return;
    changeRole.mutate(role);
  };

  if (user.role === "pending") {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={`approve-${user.id}`} className="sr-only">
          Role to approve {user.name} as
        </label>
        <RoleSelect id={`approve-${user.id}`} value={approveAs} onChange={setApproveAs} disabled={busy} />
        <button
          type="button"
          className="btn-primary btn-sm"
          disabled={busy}
          onClick={() => {
            if (approveAs !== "admin" || window.confirm(`Approve ${user.name} as an admin?`)) approve.mutate(approveAs);
          }}
        >
          <Check aria-hidden="true" className="h-4 w-4" />
          Approve
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <label htmlFor={`role-${user.id}`} className="sr-only">
        Role of {user.name}
      </label>
      <RoleSelect
        id={`role-${user.id}`}
        value={user.role === "disabled" ? "" : (user.role as Grantable)}
        placeholder={user.role === "disabled" ? "Re-enable as..." : undefined}
        onChange={pickRole}
        disabled={busy}
      />
      {user.provider === "password" && (
        <button type="button" className="btn-ghost btn-sm" disabled={busy} onClick={() => onResetPassword(user)}>
          <KeyRound aria-hidden="true" className="h-4 w-4" />
          Reset password
        </button>
      )}
      {user.role !== "disabled" && (
        <button
          type="button"
          className="btn-danger btn-sm"
          disabled={busy}
          onClick={() => window.confirm(`Disable ${user.name}? They are signed out right away.`) && disable.mutate()}
        >
          <UserX aria-hidden="true" className="h-4 w-4" />
          Disable
        </button>
      )}
    </div>
  );
}

function Who({ user, isSelf }: { user: AuthUser; isSelf: boolean }) {
  return (
    <div className="min-w-0">
      <p className="truncate font-semibold text-white">
        {user.name}
        {isSelf && <span className="ml-2 text-xs font-normal text-slate-400">(you)</span>}
      </p>
      <p className="truncate text-sm text-slate-400">{user.email}</p>
    </div>
  );
}

export default function Users() {
  const { user: me } = useAuth();
  const [filter, setFilter] = useState<Filter>("all");
  const [resetFor, setResetFor] = useState<AuthUser | null>(null);

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<UserList>("/admin/users?limit=500"),
    refetchInterval: 30_000,
  });

  const users = data?.items ?? [];
  const pending = users.filter((user) => user.role === "pending").length;
  // People waiting for a decision come first.
  const visible = users
    .filter((user) => matches(user, filter))
    .sort((a, b) => Number(b.role === "pending") - Number(a.role === "pending") || a.id - b.id);

  return (
    <>
      <PageHeader title="Users">
        {pending > 0 && <Badge tone="amber">{pending} waiting for approval</Badge>}
      </PageHeader>

      <div role="tablist" aria-label="Filter users" className="-mx-4 mb-5 flex gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:px-0">
        {FILTERS.map((item) => (
          <button
            key={item.value}
            type="button"
            role="tab"
            aria-selected={filter === item.value}
            onClick={() => setFilter(item.value)}
            className={`whitespace-nowrap rounded-full border px-4 py-2 text-sm font-medium transition ${
              filter === item.value ? "border-cyan-glow/50 bg-cyan-glow/15 text-white" : "border-white/10 text-slate-400 hover:text-white"
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {isLoading && <ListSkeleton />}
      {error && <ErrorNote error={error} retry={() => refetch()} />}
      {data && visible.length === 0 && <Empty>No users here.</Empty>}

      {visible.length > 0 && (
        <>
          {/* Phones: one card per user */}
          <ul className="space-y-3 md:hidden">
            {visible.map((user) => (
              <li key={user.id}>
                <Card>
                  <Who user={user} isSelf={user.id === me?.id} />
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <RoleBadge role={user.role} />
                    <Badge>{PROVIDER_LABELS[user.provider] ?? user.provider}</Badge>
                  </div>
                  <p className="mt-2 text-xs text-slate-500">
                    Joined {formatDateTime(user.created_at)} &middot; Last sign-in {formatDateTime(user.last_login_at)}
                  </p>
                  {user.id !== me?.id && (
                    <div className="mt-3">
                      <Actions user={user} onResetPassword={setResetFor} />
                    </div>
                  )}
                </Card>
              </li>
            ))}
          </ul>

          {/* Tablets and desktops: a table */}
          <div className="glass hidden overflow-x-auto rounded-2xl shadow-card md:block">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Dashboard users</caption>
              <thead className="border-b border-white/10 text-xs uppercase tracking-wider text-slate-400">
                <tr>
                  <th scope="col" className="px-4 py-3 font-medium">User</th>
                  <th scope="col" className="px-4 py-3 font-medium">Provider</th>
                  <th scope="col" className="px-4 py-3 font-medium">Role</th>
                  <th scope="col" className="px-4 py-3 font-medium">Last sign-in</th>
                  <th scope="col" className="px-4 py-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {visible.map((user) => (
                  <tr key={user.id} className="align-middle">
                    <td className="max-w-[16rem] px-4 py-3">
                      <Who user={user} isSelf={user.id === me?.id} />
                    </td>
                    <td className="px-4 py-3">
                      <Badge>{PROVIDER_LABELS[user.provider] ?? user.provider}</Badge>
                    </td>
                    <td className="px-4 py-3">
                      <RoleBadge role={user.role} />
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-400">{formatDateTime(user.last_login_at)}</td>
                    <td className="px-4 py-3">
                      {user.id === me?.id ? (
                        <span className="text-slate-500">This is you</span>
                      ) : (
                        <Actions user={user} onResetPassword={setResetFor} />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {resetFor && <ResetPasswordDrawer user={resetFor} onClose={() => setResetFor(null)} />}
    </>
  );
}
