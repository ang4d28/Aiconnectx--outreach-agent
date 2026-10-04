import { Loader2, LogIn, UserPlus } from "lucide-react";
import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { HttpError, api } from "../dashboard/api";
import AuthShell from "./AuthShell";
import { isActive, useAuth } from "./auth";
import type { AuthUser } from "./auth";
import WaitingApproval from "./WaitingApproval";

type Mode = "login" | "signup";

const MIN_PASSWORD = 10;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// What the backend redirects with (?auth_error=...) when Microsoft sign-in fails.
const OAUTH_ERRORS: Record<string, string> = {
  denied: "Sign-in was cancelled at the provider.",
  oauth_failed: "The sign-in could not be verified. Please try again.",
  account_conflict: "That email already belongs to a different account with this provider. Ask an administrator for help.",
  disabled: "This account is disabled. Contact an administrator.",
  unusable_profile: "The provider did not share a verified email address, so we could not use this account.",
  not_configured: "This sign-in method is not set up yet.",
};

function MicrosoftIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-5 w-5">
      <path fill="#F25022" d="M2 2h9.5v9.5H2z" />
      <path fill="#7FBA00" d="M12.5 2H22v9.5h-9.5z" />
      <path fill="#00A4EF" d="M2 12.5h9.5V22H2z" />
      <path fill="#FFB900" d="M12.5 12.5H22V22h-9.5z" />
    </svg>
  );
}

export interface Strength {
  score: 0 | 1 | 2 | 3 | 4;
  label: string;
  hint: string;
}

/** A hint, not a rule: the backend only enforces the length. */
export function passwordStrength(password: string): Strength {
  if (password.length === 0) return { score: 0, label: "", hint: `At least ${MIN_PASSWORD} characters. A few random words work well.` };
  if (password.length < MIN_PASSWORD) {
    const missing = MIN_PASSWORD - password.length;
    return { score: 0, label: "Too short", hint: `${missing} more character${missing === 1 ? "" : "s"} needed.` };
  }
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((pattern) => pattern.test(password)).length;
  const words = password.trim().split(/\s+/).length;
  let score = 1;
  if (password.length >= 14) score += 1;
  if (classes >= 3 || words >= 4) score += 1;
  if (password.length >= 20 && (classes >= 3 || words >= 4)) score += 1;
  if (/^(.)\1+$/.test(password) || /^(0123456789|1234567890|password)/i.test(password)) score = 1;
  const result = Math.min(score, 4) as 1 | 2 | 3 | 4;
  const labels = ["", "Weak", "Fair", "Good", "Strong"];
  const hints = ["", "Longer is better: try a few random words.", "Add more length or a mix of characters.", "Good enough.", "Strong password."];
  return { score: result, label: labels[result], hint: hints[result] };
}

const BAR_COLORS = ["bg-slate-600", "bg-rose-400", "bg-amber-400", "bg-cyan-glow", "bg-emerald-400"];

function StrengthMeter({ password }: { password: string }) {
  const { score, label, hint } = passwordStrength(password);
  return (
    <div id="password-hint" className="mt-2">
      <div aria-hidden="true" className="flex gap-1">
        {[1, 2, 3, 4].map((segment) => (
          <span key={segment} className={`h-1.5 flex-1 rounded-full ${segment <= score ? BAR_COLORS[score] : "bg-white/10"}`} />
        ))}
      </div>
      <p className="mt-1.5 text-xs text-slate-400" aria-live="polite">
        {label && <strong className="mr-1 text-slate-200">{label}.</strong>}
        {hint}
      </p>
    </div>
  );
}

function Field({ id, label, error, children }: { id: string; label: string; error?: string; children: ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-sm font-medium text-slate-200">
        {label}
      </label>
      {children}
      <p id={`${id}-error`} role={error ? "alert" : undefined} className="mt-1 min-h-[1.1rem] text-sm text-rose-300">
        {error}
      </p>
    </div>
  );
}

function ErrorBanner({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="mt-4 rounded-xl border border-rose-400/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
      {children}
    </p>
  );
}

function Submit({ busy, icon: Icon, children }: { busy: boolean; icon: typeof LogIn; children: ReactNode }) {
  return (
    <button type="submit" disabled={busy} className="btn-primary w-full disabled:opacity-60">
      {busy ? (
        <Loader2 aria-hidden="true" className="h-5 w-5 animate-spin motion-reduce:animate-none" />
      ) : (
        <Icon aria-hidden="true" className="h-5 w-5" />
      )}
      {children}
    </button>
  );
}

function LoginForm({ onSignedIn, adminOnly = false }: { onSignedIn: (user: AuthUser) => void; adminOnly?: boolean }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api.post<{ user: AuthUser }>("/auth/login", { email: email.trim(), password, ...(adminOnly ? { admin_only: true } : {}) });
      onSignedIn(result.user);
    } catch (err) {
      setError(
        err instanceof HttpError && err.status === 401
          ? "Wrong email or password."
          : err instanceof HttpError
            ? err.message
            : "Could not sign in.",
      );
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} noValidate aria-label={adminOnly ? "Administrator sign in" : "Sign in"} className="mt-5">
      <Field id="login-email" label="Email">
        <input
          id="login-email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className="field"
        />
      </Field>
      <Field id="login-password" label="Password">
        <input
          id="login-password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="field"
        />
      </Field>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      <div className="mt-4">
        <Submit busy={busy || !email || !password} icon={LogIn}>
          Sign in
        </Submit>
      </div>
    </form>
  );
}

function SignupForm({ onRegistered }: { onRegistered: (email: string) => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);

  const validate = (): Record<string, string> => {
    const found: Record<string, string> = {};
    if (!name.trim()) found.name = "Enter your name.";
    if (!EMAIL_PATTERN.test(email.trim())) found.email = "Enter a valid email address.";
    if (password.length < MIN_PASSWORD) found.password = `Use at least ${MIN_PASSWORD} characters.`;
    if (password.length > 128) found.password = "Use at most 128 characters.";
    return found;
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const found = validate();
    setErrors(found);
    setFormError("");
    if (Object.keys(found).length > 0) return;
    setBusy(true);
    try {
      await api.post("/auth/register", { name: name.trim(), email: email.trim(), password });
      onRegistered(email.trim().toLowerCase());
    } catch (err) {
      if (err instanceof HttpError && Object.keys(err.fieldErrors).length > 0) setErrors(err.fieldErrors);
      else setFormError(err instanceof HttpError ? err.message : "Could not create the account.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} noValidate aria-label="Create account" className="mt-5">
      <Field id="signup-name" label="Full name" error={errors.name}>
        <input
          id="signup-name"
          autoComplete="name"
          required
          value={name}
          maxLength={120}
          onChange={(event) => setName(event.target.value)}
          aria-invalid={Boolean(errors.name)}
          aria-describedby={errors.name ? "signup-name-error" : undefined}
          className="field"
        />
      </Field>
      <Field id="signup-email" label="Email" error={errors.email}>
        <input
          id="signup-email"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          aria-invalid={Boolean(errors.email)}
          aria-describedby={errors.email ? "signup-email-error" : undefined}
          className="field"
        />
      </Field>
      <div>
        <label htmlFor="signup-password" className="mb-1.5 block text-sm font-medium text-slate-200">
          Password
        </label>
        <input
          id="signup-password"
          type="password"
          autoComplete="new-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          aria-invalid={Boolean(errors.password)}
          aria-describedby={errors.password ? "password-hint signup-password-error" : "password-hint"}
          className="field"
        />
        <StrengthMeter password={password} />
        <p id="signup-password-error" role={errors.password ? "alert" : undefined} className="mt-1 min-h-[1.1rem] text-sm text-rose-300">
          {errors.password}
        </p>
      </div>
      {formError && <ErrorBanner>{formError}</ErrorBanner>}
      <p className="mt-3 text-xs text-slate-500">New accounts have no access until an administrator approves them.</p>
      <div className="mt-4">
        <Submit busy={busy} icon={UserPlus}>
          Create account
        </Submit>
      </div>
    </form>
  );
}

export default function AuthPage({ mode }: { mode: Mode }) {
  const { user, loading, setUser } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [registeredEmail, setRegisteredEmail] = useState<string | null>(null);
  // The tab only chooses which form to show; the server decides who is an administrator.
  const [admin, setAdmin] = useState(false);
  const authError = new URLSearchParams(location.search).get("auth_error");

  useEffect(() => {
    document.title = mode === "login" ? "Sign in | AI ConnectX" : "Create account | AI ConnectX";
    const robots = document.createElement("meta");
    robots.name = "robots";
    robots.content = "noindex, nofollow";
    document.head.appendChild(robots);
    return () => {
      robots.remove();
    };
  }, [mode]);

  if (!loading && isActive(user)) return <Navigate to="/dashboard" replace />;
  if (!loading && user) return <Navigate to="/waiting" replace />;
  if (registeredEmail) return <WaitingApproval registered={{ email: registeredEmail }} onLeave={() => setRegisteredEmail(null)} />;

  const signedIn = (next: AuthUser) => {
    setUser(next);
    navigate(isActive(next) ? "/dashboard" : "/waiting", { replace: true });
  };

  return (
    <AuthShell wide>
      <div role="tablist" aria-label="Account type" className="mt-8 grid grid-cols-2 rounded-xl border border-white/10 bg-white/[0.03] p-1">
        {([false, true] as const).map((isAdminTab) => (
          <button
            key={String(isAdminTab)}
            type="button"
            role="tab"
            aria-selected={admin === isAdminTab}
            onClick={() => {
              setAdmin(isAdminTab);
              if (isAdminTab && mode === "signup") navigate("/login", { replace: true });
            }}
            className={`rounded-lg px-3 py-2 text-center text-sm font-semibold transition ${
              admin === isAdminTab ? "bg-white/10 text-white" : "text-slate-400 hover:text-white"
            }`}
          >
            {isAdminTab ? "Admin" : "Member"}
          </button>
        ))}
      </div>

      <h1 className="mt-6 text-2xl font-bold text-white">
        {admin ? "Administrator sign in" : mode === "login" ? "Member sign in" : "Create your account"}
      </h1>
      <p className="mt-1 text-sm text-slate-400">
        {admin
          ? "Email and password only. Only administrator accounts can sign in here."
          : mode === "login"
            ? "Sign in to the AI ConnectX dashboard."
            : "Sign up, then wait for an administrator to approve you."}
      </p>

      {authError && !admin && <ErrorBanner>{OAUTH_ERRORS[authError] ?? "Sign-in failed. Please try again."}</ErrorBanner>}

      {admin ? (
        <LoginForm onSignedIn={signedIn} adminOnly />
      ) : (
        <>
          <div className="mt-5 grid gap-3">
            <a href="/api/auth/microsoft/login" className="btn-ghost w-full">
              <MicrosoftIcon />
              Continue with Microsoft
            </a>
          </div>

          <div className="my-5 flex items-center gap-3 text-xs uppercase tracking-wider text-slate-500" role="separator">
            <span className="h-px flex-1 bg-white/10" />
            or with email
            <span className="h-px flex-1 bg-white/10" />
          </div>

          <div role="tablist" aria-label="Sign in or create an account" className="grid grid-cols-2 rounded-xl border border-white/10 bg-white/[0.03] p-1">
            {(["login", "signup"] as const).map((tab) => (
              <Link
                key={tab}
                to={tab === "login" ? "/login" : "/signup"}
                role="tab"
                aria-selected={mode === tab}
                replace
                className={`rounded-lg px-3 py-2 text-center text-sm font-semibold transition ${
                  mode === tab ? "bg-white/10 text-white" : "text-slate-400 hover:text-white"
                }`}
              >
                {tab === "login" ? "Sign in" : "Create account"}
              </Link>
            ))}
          </div>

          {mode === "login" ? <LoginForm onSignedIn={signedIn} /> : <SignupForm onRegistered={setRegisteredEmail} />}
        </>
      )}
    </AuthShell>
  );
}
