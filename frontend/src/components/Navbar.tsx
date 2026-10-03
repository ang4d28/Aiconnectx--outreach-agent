import { Menu, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { NAV_LINKS } from "../lib/content";
import Logo from "./Logo";

export default function Navbar() {
  const [open, setOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = previous;
      window.removeEventListener("keydown", onKey);
      toggleRef.current?.focus();
    };
  }, [open]);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 768px)");
    const onChange = () => media.matches && setOpen(false);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  return (
    <>
      <header
        className={`sticky top-0 z-50 border-b transition-colors ${
          scrolled
            ? "border-white/10 bg-navy-950/70 backdrop-blur-xl"
            : "border-transparent bg-navy-950/30 backdrop-blur-md"
        }`}
      >
        <div className="container-x flex h-16 items-center justify-between">
          <a href="#top" aria-label="AI ConnectX home">
            <Logo />
          </a>

          <nav aria-label="Primary" className="hidden items-center gap-8 md:flex">
            {NAV_LINKS.map((link) => (
              <a
                key={link.href}
                href={link.href}
                className="text-sm font-medium text-slate-300 transition hover:text-white"
              >
                {link.label}
              </a>
            ))}
            <Link to="/login" className="text-sm font-medium text-slate-300 transition hover:text-white">
              Member login
            </Link>
            <a href="#contact" className="btn-primary !min-h-0 !px-5 !py-2 text-sm">
              Contact us
            </a>
          </nav>

          <button
            ref={toggleRef}
            type="button"
            className="-mr-2 inline-flex h-11 w-11 items-center justify-center rounded-lg text-white hover:bg-white/10 md:hidden"
            aria-label="Open menu"
            aria-expanded={open}
            aria-controls="mobile-drawer"
            onClick={() => setOpen(true)}
          >
            <Menu aria-hidden="true" className="h-6 w-6" />
          </button>
        </div>
      </header>

      {/* Outside the header: its backdrop-filter would otherwise become the containing block of these fixed elements. */}
      <div className={`md:hidden ${open ? "" : "pointer-events-none"}`}>
        <div
          className={`fixed inset-0 z-40 bg-navy-950/70 backdrop-blur-sm transition-opacity motion-reduce:transition-none ${
            open ? "opacity-100" : "opacity-0"
          }`}
          onClick={() => setOpen(false)}
          aria-hidden="true"
        />
        <div
          id="mobile-drawer"
          role="dialog"
          aria-modal="true"
          aria-label="Menu"
          aria-hidden={!open}
          className={`fixed inset-y-0 right-0 z-50 flex w-[min(20rem,85vw)] flex-col border-l border-white/10 bg-navy-900/95 p-6 backdrop-blur-xl transition-transform duration-300 motion-reduce:transition-none ${
            open ? "translate-x-0" : "invisible translate-x-full"
          }`}
        >
          <div className="mb-8 flex items-center justify-between">
            <Logo />
            <button
              ref={closeRef}
              type="button"
              className="-mr-2 inline-flex h-11 w-11 items-center justify-center rounded-lg text-white hover:bg-white/10"
              aria-label="Close menu"
              onClick={() => setOpen(false)}
            >
              <X aria-hidden="true" className="h-6 w-6" />
            </button>
          </div>
          <nav aria-label="Mobile" className="flex flex-col gap-1">
            {NAV_LINKS.map((link) => (
              <a
                key={link.href}
                href={link.href}
                onClick={() => setOpen(false)}
                className="rounded-lg px-3 py-3 text-lg font-medium text-slate-200 hover:bg-white/5 hover:text-white"
              >
                {link.label}
              </a>
            ))}
          </nav>
          <Link
            to="/login"
            onClick={() => setOpen(false)}
            className="mt-2 rounded-lg px-3 py-3 text-lg font-medium text-slate-200 hover:bg-white/5 hover:text-white"
          >
            Member login
          </Link>
          <a href="#contact" onClick={() => setOpen(false)} className="btn-primary mt-auto">
            Contact us
          </a>
        </div>
      </div>
    </>
  );
}
