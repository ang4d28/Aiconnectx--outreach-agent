import { BENEFITS } from "../lib/content";
import Section from "./Section";

export default function WhyPartner() {
  return (
    <Section
      id="why-partner"
      eyebrow="Why partner"
      title="Hire smarter with a campus that builds AI talent"
      intro="Everything you need to bring AI/ML graduates into your team, without the usual overhead."
    >
      <ul className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
        {BENEFITS.map((benefit) => (
          <li
            key={benefit.title}
            className="glass rounded-2xl p-6 shadow-card transition hover:-translate-y-1 hover:border-cyan-glow/40 motion-reduce:transition-none motion-reduce:hover:translate-y-0"
          >
            <span className="flex h-11 w-11 items-center justify-center rounded-xl border border-cyan-glow/30 bg-cyan-glow/10 text-cyan-glow">
              <benefit.icon aria-hidden="true" className="h-5 w-5" />
            </span>
            <h3 className="mt-5 text-lg font-semibold text-white">{benefit.title}</h3>
            <p className="mt-2 leading-relaxed text-slate-400">{benefit.text}</p>
          </li>
        ))}
      </ul>
    </Section>
  );
}
