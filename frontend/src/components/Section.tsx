import type { ReactNode } from "react";

interface Props {
  id: string;
  eyebrow: string;
  title: string;
  intro?: string;
  children: ReactNode;
}

export default function Section({ id, eyebrow, title, intro, children }: Props) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="py-20 sm:py-28">
      <div className="container-x">
        <header className="mx-auto max-w-2xl text-center">
          <p className="text-sm font-semibold uppercase tracking-widest text-cyan-glow">{eyebrow}</p>
          <h2 id={`${id}-title`} className="mt-3 text-3xl font-bold tracking-tight text-white sm:text-4xl">
            {title}
          </h2>
          {intro && <p className="mt-4 text-lg text-slate-400">{intro}</p>}
        </header>
        <div className="mt-12 sm:mt-16">{children}</div>
      </div>
    </section>
  );
}
