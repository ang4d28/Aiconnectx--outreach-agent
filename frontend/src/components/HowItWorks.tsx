import { STEPS } from "../lib/content";
import Section from "./Section";

export default function HowItWorks() {
  return (
    <Section
      id="how-it-works"
      eyebrow="How it works"
      title="From first message to first hire in three steps"
      intro="No long campus drives. Just a clear path from your hiring need to the right graduates."
    >
      <ol className="grid gap-6 md:grid-cols-3">
        {STEPS.map((step, index) => (
          <li key={step.title} className="glass relative rounded-2xl p-6 shadow-card sm:p-8">
            <span
              aria-hidden="true"
              className="gradient-text absolute right-6 top-4 text-5xl font-extrabold opacity-30"
            >
              {index + 1}
            </span>
            <span className="flex h-12 w-12 items-center justify-center rounded-xl bg-accent-gradient text-navy-950">
              <step.icon aria-hidden="true" className="h-6 w-6" />
            </span>
            <h3 className="mt-6 text-xl font-semibold text-white">
              <span className="sr-only">Step {index + 1}: </span>
              {step.title}
            </h3>
            <p className="mt-3 leading-relaxed text-slate-400">{step.text}</p>
          </li>
        ))}
      </ol>
    </Section>
  );
}
