import { Clock, Loader2, LogOut, MailCheck, ShieldOff } from "lucide-react";
import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import AuthShell from "./AuthShell";
import { isActive, useAuth } from "./auth";

interface Props {
  /** Shown right after signing up with a password, before the person can sign in. */
  registered?: { email: string };
  /** Called after sign-out so the sign-up screen forgets the registration. */
  onLeave?: () => void;
}

export default function WaitingApproval({ registered, onLeave }: Props) {
  const { user, refresh, logout } = useAuth();
  const navigate = useNavigate();
  const [checking, setChecking] = useState(false);
  const [stillWaiting, setStillWaiting] = useState(false);

  // End the session (POST /api/auth/logout), clear the client state, then show the sign-in page.
  const leave = async () => {
    await logout();
    onLeave?.();
    navigate("/login", { replace: true });
  };

  if (!registered && isActive(user)) return <Navigate to="/dashboard" replace />;
  if (!registered && !user) return <Navigate to="/login" replace />;

  const disabled = user?.role === "disabled";
  const recheck = async () => {
    setChecking(true);
    const me = await refresh();
    setStillWaiting(!isActive(me));
    setChecking(false);
  };

  if (registered) {
    return (
      <AuthShell wide>
        <MailCheck aria-hidden="true" className="mt-8 h-10 w-10 text-cyan-glow" />
        <h1 className="mt-4 text-2xl font-bold text-white">Account created. Waiting for approval</h1>
        <p className="mt-3 text-sm leading-relaxed text-slate-300">
          Thanks for signing up. An administrator has to approve <strong className="break-words text-white [overflow-wrap:anywhere]">{registered.email}</strong>{" "}
          before you can open the dashboard. Nobody can see anything until then.
        </p>
        <p className="mt-3 text-sm leading-relaxed text-slate-400">
          Once approved, sign in with the same email and password.
        </p>
        <button type="button" onClick={() => void leave()} className="btn-primary mt-6 w-full">
          Go to sign in
        </button>
      </AuthShell>
    );
  }

  return (
    <AuthShell wide>
      {disabled ? (
        <ShieldOff aria-hidden="true" className="mt-8 h-10 w-10 text-rose-300" />
      ) : (
        <Clock aria-hidden="true" className="mt-8 h-10 w-10 text-amber-300" />
      )}
      <h1 className="mt-4 text-2xl font-bold text-white">{disabled ? "Your account is disabled" : "Waiting for approval"}</h1>
      <p className="mt-3 text-sm leading-relaxed text-slate-300">
        {disabled
          ? "An administrator turned off this account, so you cannot use the dashboard. Contact them if you think this is a mistake."
          : "You are signed in, but an administrator has to approve your access before you can see anything. You do not need to do anything else."}
      </p>
      {user && (
        <p className="mt-4 rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3 text-sm text-slate-300">
          Signed in as <strong className="text-white">{user.name}</strong>
          <span className="block break-words text-slate-400 [overflow-wrap:anywhere]">{user.email}</span>
        </p>
      )}
      <p role="status" className="mt-3 min-h-[1.25rem] text-sm text-amber-200">
        {stillWaiting && !disabled ? "Not approved yet. Check again later." : ""}
      </p>
      <div className="mt-3 flex flex-col gap-3 sm:flex-row">
        {!disabled && (
          <button type="button" onClick={recheck} disabled={checking} className="btn-primary flex-1 disabled:opacity-60">
            {checking && <Loader2 aria-hidden="true" className="h-5 w-5 animate-spin motion-reduce:animate-none" />}
            Check again
          </button>
        )}
        <button type="button" onClick={() => void leave()} className="btn-ghost flex-1">
          <LogOut aria-hidden="true" className="h-5 w-5" />
          Go to sign in
        </button>
      </div>
      <p className="mt-4 text-center text-sm text-slate-400">
        Not you?{" "}
        <button type="button" onClick={() => void leave()} className="font-semibold text-cyan-glow underline">
          Sign out
        </button>
      </p>
    </AuthShell>
  );
}
