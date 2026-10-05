import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { HttpError, api } from "./api";
import type { SettingsPayload, SettingsValues } from "./api";
import ChannelsSection from "./Channels";
import { Card, ErrorNote, PageHeader, Spinner, toast } from "./ui";

type Form = Record<keyof SettingsValues, string>;

function toForm(values: SettingsValues): Form {
  return Object.fromEntries(Object.entries(values).map(([key, value]) => [key, String(value)])) as Form;
}

function Field({
  id,
  label,
  hint,
  error,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-sm font-medium text-slate-200">
        {label}
      </label>
      {children}
      {hint && !error && <p className="mt-1.5 text-xs text-slate-500">{hint}</p>}
      {error && (
        <p id={`${id}-error`} className="mt-1.5 text-sm text-rose-300">
          {error}
        </p>
      )}
    </div>
  );
}

export default function Settings() {
  const client = useQueryClient();
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<SettingsPayload>("/dashboard/settings"),
  });

  const [form, setForm] = useState<Form | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [queries, setQueries] = useState("");

  useEffect(() => {
    if (data) {
      setForm(toForm(data.values));
      setQueries(data.search_queries.join("\n"));
    }
  }, [data]);

  const apply = (payload: SettingsPayload) => {
    client.setQueryData(["settings"], payload);
    client.invalidateQueries({ queryKey: ["status"] });
  };

  const save = useMutation({
    mutationFn: (body: { values: Record<string, unknown>; confirm_live?: boolean }) =>
      api.put<SettingsPayload>("/dashboard/settings", body),
    onSuccess: (payload) => {
      setFieldErrors({});
      apply(payload);
      toast("Settings saved. The scheduler picks them up on its next job.");
    },
    onError: (err) => {
      if (err instanceof HttpError && Object.keys(err.fieldErrors).length > 0) setFieldErrors(err.fieldErrors);
      toast(err instanceof Error ? err.message : "Could not save", "error");
    },
  });

  const saveQueries = useMutation({
    mutationFn: (list: string[]) => api.put<SettingsPayload>("/dashboard/search-queries", { queries: list }),
    onSuccess: (payload) => {
      apply(payload);
      toast("Search queries saved. The next post monitor run uses them.");
    },
    onError: (err) => toast(err instanceof Error ? err.message : "Could not save queries", "error"),
  });

  if (isLoading || !form) return error ? <ErrorNote error={error} retry={() => refetch()} /> : <Spinner />;
  if (!data) return <ErrorNote error={error} retry={() => refetch()} />;

  const set = (key: keyof SettingsValues) => (event: { target: { value: string } }) =>
    setForm((current) => (current ? { ...current, [key]: event.target.value } : current));

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const values: Record<string, unknown> = {
      linkedin_daily_cap: Number(form.linkedin_daily_cap),
      email_daily_cap: Number(form.email_daily_cap),
      send_window_start: form.send_window_start,
      send_window_end: form.send_window_end,
      hold_window_minutes: Number(form.hold_window_minutes),
      tpo_name: form.tpo_name,
      tpo_phone: form.tpo_phone,
      tpo_email: form.tpo_email,
    };
    save.mutate({ values });
  };

  const dryRun = data.values.dry_run;
  const toggleDryRun = () => {
    if (dryRun) {
      const ok = window.confirm(
        "Turn DRY RUN off? From the next run, approved messages are sent to real people on LinkedIn and email.",
      );
      if (ok) save.mutate({ values: { dry_run: false }, confirm_live: true });
    } else {
      save.mutate({ values: { dry_run: true } });
    }
  };

  const input = (key: keyof SettingsValues, type = "text", extra: Record<string, unknown> = {}) => (
    <input
      id={key}
      name={key}
      type={type}
      value={form[key]}
      onChange={set(key)}
      aria-invalid={Boolean(fieldErrors[key])}
      aria-describedby={fieldErrors[key] ? `${key}-error` : undefined}
      className="field"
      {...extra}
    />
  );

  return (
    <>
      <PageHeader title="Settings" />
      <div className="grid gap-5 xl:grid-cols-2">
        <div className="xl:col-span-2">
          <ChannelsSection />
        </div>

        <Card className="xl:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold text-white">Dry run</h2>
              <p className="mt-1 max-w-xl text-sm text-slate-400">
                {dryRun
                  ? "On: nothing is sent to leads. Drafts, previews and reports still work."
                  : "Off: approved messages are being sent to real people."}
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={dryRun}
              aria-label="Dry run"
              disabled={save.isPending}
              onClick={toggleDryRun}
              className={`flex items-center gap-3 rounded-full border px-4 py-2 text-sm font-semibold ${
                dryRun ? "border-amber-400/50 bg-amber-400/15 text-amber-200" : "border-rose-400/50 bg-rose-500/15 text-rose-200"
              }`}
            >
              <span aria-hidden="true" className={`relative h-5 w-9 rounded-full ${dryRun ? "bg-amber-400" : "bg-rose-500"}`}>
                <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${dryRun ? "left-[1.1rem]" : "left-0.5"}`} />
              </span>
              {dryRun ? "DRY RUN" : "LIVE"}
            </button>
          </div>
        </Card>

        <form onSubmit={submit} className="space-y-5 xl:col-span-2">
          <div className="grid gap-5 lg:grid-cols-2">
            <Card>
              <h2 className="mb-4 text-lg font-semibold text-white">Limits and timing</h2>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field id="linkedin_daily_cap" label="LinkedIn actions per day" error={fieldErrors.linkedin_daily_cap} hint="Shared by comments, notes and DMs.">
                  {input("linkedin_daily_cap", "number", { min: 1, max: 100, inputMode: "numeric" })}
                </Field>
                <Field id="email_daily_cap" label="Emails per day" error={fieldErrors.email_daily_cap} hint="Max. The first 14 days still ramp at 20.">
                  {input("email_daily_cap", "number", { min: 1, max: 200, inputMode: "numeric" })}
                </Field>
                <Field id="send_window_start" label="Send window opens (IST)" error={fieldErrors.send_window_start}>
                  {input("send_window_start", "time")}
                </Field>
                <Field
                  id="send_window_end"
                  label="Send window closes (IST)"
                  error={fieldErrors.send_window_end}
                  hint="A wider window needs a scheduler restart to take effect."
                >
                  {input("send_window_end", "time")}
                </Field>
                <Field id="hold_window_minutes" label="Hold window (minutes)" error={fieldErrors.hold_window_minutes} hint="Time you have to cancel or edit before a message sends.">
                  {input("hold_window_minutes", "number", { min: 1, max: 240, inputMode: "numeric" })}
                </Field>
              </div>
            </Card>

            <Card>
              <h2 className="mb-1 text-lg font-semibold text-white">Placement officer details</h2>
              <p className="mb-4 text-sm text-slate-400">Shared with companies that say they are interested.</p>
              <div className="grid gap-4">
                <Field id="tpo_name" label="Name" error={fieldErrors.tpo_name}>
                  {input("tpo_name", "text", { autoComplete: "off", maxLength: 120 })}
                </Field>
                <Field id="tpo_phone" label="Phone" error={fieldErrors.tpo_phone}>
                  {input("tpo_phone", "tel", { autoComplete: "off", maxLength: 30 })}
                </Field>
                <Field id="tpo_email" label="Email" error={fieldErrors.tpo_email}>
                  {input("tpo_email", "email", { autoComplete: "off", maxLength: 254 })}
                </Field>
              </div>
            </Card>
          </div>
          <button type="submit" disabled={save.isPending} className="btn-primary">
            Save settings
          </button>
        </form>

        <Card className="xl:col-span-2">
          <h2 className="text-lg font-semibold text-white">Search queries</h2>
          <p className="mb-4 mt-1 text-sm text-slate-400">
            One query per line, used by the post monitor to find LinkedIn hiring posts (up to 30).
          </p>
          <label htmlFor="queries" className="sr-only">
            Search queries
          </label>
          <textarea
            id="queries"
            rows={8}
            value={queries}
            onChange={(event) => setQueries(event.target.value)}
            spellCheck={false}
            className="field font-mono text-sm"
          />
          <button
            type="button"
            className="btn-primary mt-3"
            disabled={saveQueries.isPending}
            onClick={() => saveQueries.mutate(queries.split("\n"))}
          >
            Save queries
          </button>
        </Card>
      </div>
    </>
  );
}
