import { ArrowRight, Sparkles } from "lucide-react";
import { STATS } from "../lib/content";
import type { Interest } from "../types";
import NetworkBackground from "./NetworkBackground";

interface Props {
  onChoose: (interest: Interest) => void;
}

const CHIP_POSITIONS = [
  "lg:right-[6%] lg:top-[22%]",
  "lg:right-[24%] lg:top-[48%] [animation-delay:1.5s]",
  "lg:right-[8%] lg:top-[68%] [animation-delay:3s]",
];

export default function Hero({ onChoose }: Props) {
  return (
    <section
      id="top"
      aria-labelledby="hero-title"
      className="relative isolate -mt-16 flex min-h-[100svh] items-center overflow-hidden pb-16 pt-28 sm:pt-32"
    >
      <div className="absolute inset-0 -z-10" aria-hidden="true">
        <div className="absolute -right-32 -top-32 h-[34rem] w-[34rem] rounded-full bg-violet-glow/25 blur-[120px]" />
        <div className="absolute -bottom-40 -left-24 h-[30rem] w-[30rem] rounded-full bg-cyan-glow/15 blur-[120px]" />
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,transparent_30%,#0A0F1F_85%)]" />
        <NetworkBackground />
      </div>

      <div className="container-x relative">
        <div className="max-w-3xl">
          <p className="inline-flex items-center gap-2 rounded-full border border-cyan-glow/30 bg-cyan-glow/10 px-4 py-1.5 text-sm font-medium text-cyan-100 motion-safe:animate-rise">
            <Sparkles aria-hidden="true" className="h-4 w-4 text-cyan-glow" />
            AI/ML talent partnerships
          </p>
          <h1
            id="hero-title"
            className="mt-6 text-[2.5rem] font-extrabold leading-[1.05] tracking-tight text-white [animation-delay:80ms] motion-safe:animate-rise sm:text-6xl lg:text-7xl"
          >
            Where campuses <span className="gradient-text">meet careers.</span>
          </h1>
          <p className="mt-6 max-w-2xl text-lg leading-relaxed text-slate-300 [animation-delay:160ms] motion-safe:animate-rise sm:text-xl">
            AI ConnectX connects companies and startups with AI/ML talent from Universal AI University, so you can
            hire skilled graduates and build lasting campus partnerships.
          </p>
          <div className="mt-9 flex flex-col gap-3 [animation-delay:240ms] motion-safe:animate-rise sm:flex-row">
            <a href="#contact" className="btn-primary" onClick={() => onChoose("hire")}>
              Hire from our campus
              <ArrowRight aria-hidden="true" className="h-5 w-5" />
            </a>
            <a href="#contact" className="btn-ghost" onClick={() => onChoose("partner")}>
              Partner with us
            </a>
          </div>
        </div>

        {STATS.length > 0 && (
          <ul
            aria-label="AI ConnectX at a glance"
            className="mt-12 grid max-w-xl grid-cols-1 gap-3 min-[420px]:grid-cols-3 lg:mt-0 lg:block lg:max-w-none"
          >
            {STATS.map((item, index) => (
              <li
                key={item.label}
                className={`glass rounded-2xl px-4 py-3 shadow-card lg:absolute motion-safe:lg:animate-float ${CHIP_POSITIONS[index % CHIP_POSITIONS.length]}`}
              >
                <p className="gradient-text text-2xl font-extrabold leading-none sm:text-3xl">{item.value}</p>
                <p className="mt-1 text-xs font-medium text-slate-300 sm:text-sm">{item.label}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
