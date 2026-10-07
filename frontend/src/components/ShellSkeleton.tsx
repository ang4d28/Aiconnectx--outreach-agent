/**
 * Placeholder for the dashboard shell (sidebar, header, a few cards) shown while a route chunk or the session
 * check loads, so the screen is never blank. Plain markup only: it is part of the first bundle.
 */
function Block({ className }: { className: string }) {
  return <div aria-hidden="true" className={`animate-pulse rounded-xl bg-white/[0.06] motion-reduce:animate-none ${className}`} />;
}

export default function ShellSkeleton({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" aria-busy="true" className="min-h-screen md:pl-60">
      <span className="sr-only">{label}</span>
      <aside className="fixed inset-y-0 left-0 hidden w-60 flex-col gap-2 border-r border-white/10 bg-navy-900/80 p-4 md:flex">
        <Block className="mb-6 h-9 w-36" />
        {[0, 1, 2, 3, 4, 5].map((n) => (
          <Block key={n} className="h-10 w-full" />
        ))}
      </aside>
      <header className="flex h-14 items-center justify-between border-b border-white/10 px-4 sm:px-6">
        <Block className="h-5 w-28" />
        <Block className="h-8 w-40" />
      </header>
      <main className="px-4 pt-6 sm:px-6">
        <Block className="mb-6 h-9 w-48" />
        <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-5">
          {[0, 1, 2, 3, 4].map((n) => (
            <Block key={n} className="h-28" />
          ))}
        </div>
        <Block className="mt-6 h-64" />
      </main>
    </div>
  );
}
