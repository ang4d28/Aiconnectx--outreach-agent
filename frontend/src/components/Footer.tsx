import { Link } from "react-router-dom";
import { NAV_LINKS } from "../lib/content";
import Logo from "./Logo";

export default function Footer() {
  return (
    <footer className="border-t border-white/10 bg-navy-900/60">
      <div className="container-x flex flex-col gap-8 py-12 md:flex-row md:items-start md:justify-between">
        <div className="max-w-sm">
          <Logo />
          <p className="mt-4 text-sm leading-relaxed text-slate-400">
            Connecting companies and startups with AI/ML talent from Universal AI University.
          </p>
        </div>
        <nav aria-label="Footer">
          <ul className="grid grid-cols-2 gap-x-10 gap-y-3 text-sm">
            {NAV_LINKS.map((link) => (
              <li key={link.href}>
                <a href={link.href} className="text-slate-300 transition hover:text-white">
                  {link.label}
                </a>
              </li>
            ))}
            <li>
              <a href="#contact" className="text-slate-300 transition hover:text-white">
                Contact
              </a>
            </li>
            <li>
              <Link to="/login" className="text-slate-300 transition hover:text-white">
                Member login
              </Link>
            </li>
          </ul>
        </nav>
      </div>
      <div className="border-t border-white/10 py-6 text-center text-xs text-slate-500">
        &copy; {new Date().getFullYear()} AI ConnectX. All rights reserved.
      </div>
    </footer>
  );
}
