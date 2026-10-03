import { FileSignature } from "lucide-react";
import type { Interest } from "../types";

interface Props {
  onChoose: (interest: Interest) => void;
}

export default function MouBanner({ onChoose }: Props) {
  return (
    <section id="mou" aria-labelledby="mou-title" className="py-12 sm:py-16">
      <div className="container-x">
        <div className="relative isolate overflow-hidden rounded-3xl border border-white/10 bg-navy-800 px-6 py-12 text-center sm:px-12 sm:py-16">
          <div
            aria-hidden="true"
            className="absolute -left-20 -top-24 -z-10 h-72 w-72 rounded-full bg-violet-glow/30 blur-[90px]"
          />
          <div
            aria-hidden="true"
            className="absolute -bottom-24 -right-16 -z-10 h-72 w-72 rounded-full bg-cyan-glow/25 blur-[90px]"
          />
          <FileSignature aria-hidden="true" className="mx-auto h-10 w-10 text-cyan-glow" />
          <h2
            id="mou-title"
            className="mx-auto mt-5 max-w-2xl text-3xl font-bold tracking-tight text-white sm:text-4xl"
          >
            Make it official with a campus MoU
          </h2>
          <p className="mx-auto mt-4 max-w-2xl text-lg text-slate-300">
            A Memorandum of Understanding gives your company structured access to internships, projects and
            full-time hiring from Universal AI University. There is no obligation to start the conversation.
          </p>
          <a href="#contact" className="btn-primary mt-8" onClick={() => onChoose("partner")}>
            Start an MoU conversation
          </a>
        </div>
      </div>
    </section>
  );
}
