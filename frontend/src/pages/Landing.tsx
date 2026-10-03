import { useState } from "react";
import ContactForm from "../components/ContactForm";
import Footer from "../components/Footer";
import Hero from "../components/Hero";
import HowItWorks from "../components/HowItWorks";
import MouBanner from "../components/MouBanner";
import Navbar from "../components/Navbar";
import Roles from "../components/Roles";
import WhyPartner from "../components/WhyPartner";
import type { Interest } from "../types";

export default function Landing() {
  const [interest, setInterest] = useState<Interest>("hire");

  return (
    <>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[60] focus:rounded-lg focus:bg-white focus:px-4 focus:py-2 focus:text-navy-950"
      >
        Skip to content
      </a>
      <Navbar />
      <main id="main">
        <Hero onChoose={setInterest} />
        <HowItWorks />
        <WhyPartner />
        <Roles />
        <MouBanner onChoose={setInterest} />
        <ContactForm interest={interest} onInterestChange={setInterest} />
      </main>
      <Footer />
    </>
  );
}
