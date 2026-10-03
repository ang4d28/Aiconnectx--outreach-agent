import { ROLES } from "../lib/content";
import Section from "./Section";

export default function Roles() {
  return (
    <Section
      id="roles"
      eyebrow="Roles"
      title="Roles our students fit"
      intro="From research internships to production engineering."
    >
      <ul className="mx-auto flex max-w-4xl flex-wrap justify-center gap-3">
        {ROLES.map((role) => (
          <li
            key={role}
            className="glass rounded-full px-5 py-2.5 text-sm font-medium text-slate-200 transition hover:border-cyan-glow/50 hover:text-white sm:text-base"
          >
            {role}
          </li>
        ))}
      </ul>
    </Section>
  );
}
