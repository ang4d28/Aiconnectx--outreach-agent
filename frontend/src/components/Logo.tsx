import { useId } from "react";

interface Props {
  className?: string;
  iconOnly?: boolean;
}

export default function Logo({ className = "", iconOnly = false }: Props) {
  // Unique per instance: a gradient inside a display:none copy would otherwise be the one every copy resolves to.
  const gradient = `logo-${useId().replace(/:/g, "")}`;
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <svg viewBox="0 0 32 32" className="h-8 w-8 shrink-0" role="img" aria-label="AI ConnectX logo">
        <defs>
          <linearGradient id={gradient} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#22D3EE" />
            <stop offset="0.6" stopColor="#3B82F6" />
            <stop offset="1" stopColor="#8B5CF6" />
          </linearGradient>
        </defs>
        <rect width="32" height="32" rx="8" fill="#121A36" />
        <path
          d="M8 22 16 10l8 12M10.5 18h11"
          stroke={`url(#${gradient})`}
          strokeWidth="2"
          strokeLinecap="round"
          fill="none"
        />
        <g fill={`url(#${gradient})`}>
          <circle cx="8" cy="22" r="2.4" />
          <circle cx="16" cy="10" r="2.4" />
          <circle cx="24" cy="22" r="2.4" />
        </g>
      </svg>
      <span className={`whitespace-nowrap text-lg font-bold tracking-tight text-white ${iconOnly ? "sr-only" : ""}`}>
        AI Connect<span className="gradient-text">X</span>
      </span>
    </span>
  );
}
