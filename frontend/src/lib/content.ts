import { BrainCircuit, Briefcase, Handshake, Rocket, Search, Users } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { Interest } from "../types";

export const NAV_LINKS = [
  { href: "#how-it-works", label: "How it works" },
  { href: "#why-partner", label: "Why partner" },
  { href: "#roles", label: "Roles" },
  { href: "#mou", label: "MoU" },
] as const;

export interface Stat {
  label: string;
  value: string;
}

// Real figures come from the build environment (frontend/.env.local). Nothing is
// invented: in production an unset figure hides its chip. Dev builds show
// placeholders so the layout can be reviewed.
function stat(label: string, configured: string | undefined, devPlaceholder: string): Stat | null {
  const value = configured?.trim() || (import.meta.env.DEV ? devPlaceholder : "");
  return value ? { label, value } : null;
}

export const STATS: Stat[] = [
  stat("Companies connected", import.meta.env.VITE_STAT_COMPANIES, "00+"),
  stat("Students", import.meta.env.VITE_STAT_STUDENTS, "000+"),
  stat("MoUs signed", import.meta.env.VITE_STAT_MOUS, "00"),
].filter((item): item is Stat => item !== null);

export interface Card {
  title: string;
  text: string;
  icon: LucideIcon;
}

export const STEPS: Card[] = [
  {
    title: "Tell us what you need",
    text: "Share the roles, skills and timelines you are hiring for. A short note is enough to start.",
    icon: Search,
  },
  {
    title: "We shortlist and connect",
    text: "We match your needs with AI/ML students from Universal AI University and introduce you to the best fits.",
    icon: Users,
  },
  {
    title: "Interview, hire, formalise",
    text: "Interview on your terms, make offers, and put a Memorandum of Understanding in place for repeat hiring.",
    icon: Handshake,
  },
];

export const BENEFITS: Card[] = [
  {
    title: "AI/ML-focused talent",
    text: "Students trained in machine learning, data and modern software engineering, ready for real projects.",
    icon: BrainCircuit,
  },
  {
    title: "Faster, lighter hiring",
    text: "Pre-screened shortlists and one point of contact save your team the cost of broad campus drives.",
    icon: Rocket,
  },
  {
    title: "Structured partnerships",
    text: "An MoU gives you a clear, repeatable path to internships, projects and full-time hiring.",
    icon: Handshake,
  },
  {
    title: "Built for startups too",
    text: "You do not need a large HR team. Founders and small teams get the same access as established companies.",
    icon: Briefcase,
  },
];

export const ROLES = [
  "AI / ML Engineer",
  "Data Scientist",
  "Software Developer",
  "NLP & LLM Engineer",
  "Computer Vision Engineer",
  "Data Engineer",
  "MLOps Engineer",
  "Data Analyst",
  "Research Intern",
  "Full-Stack Developer",
] as const;

export const INTERESTS: { value: Interest; label: string }[] = [
  { value: "hire", label: "Hire from campus" },
  { value: "partner", label: "Partner with us (MoU)" },
  { value: "internships", label: "Internships" },
  { value: "other", label: "Something else" },
];
