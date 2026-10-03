import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import Logo from "../../components/Logo";

/** Page frame shared by the sign-in and waiting screens: the landing page glow and glass card. */
export default function AuthShell({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <main className="relative isolate flex min-h-screen flex-col items-center justify-center px-4 py-10 sm:py-12">
      <div aria-hidden="true" className="absolute -right-24 -top-24 -z-10 h-96 w-96 rounded-full bg-violet-glow/25 blur-[110px]" />
      <div aria-hidden="true" className="absolute -bottom-24 -left-16 -z-10 h-80 w-80 rounded-full bg-cyan-glow/15 blur-[110px]" />
      <div className={`glass w-full rounded-3xl p-6 shadow-card sm:p-8 ${wide ? "max-w-md" : "max-w-sm"}`}>
        <Link to="/" aria-label="AI ConnectX home" className="inline-block">
          <Logo />
        </Link>
        {children}
      </div>
      <Link to="/" className="mt-6 text-sm text-slate-400 transition hover:text-white">
        &larr; Back to the website
      </Link>
    </main>
  );
}
