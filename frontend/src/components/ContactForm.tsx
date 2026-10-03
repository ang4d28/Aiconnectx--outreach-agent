import { CheckCircle2, Loader2 } from "lucide-react";
import { useState } from "react";
import type { FormEvent } from "react";
import { submitInquiry } from "../lib/api";
import { INTERESTS } from "../lib/content";
import type { InquiryPayload, Interest } from "../types";
import Section from "./Section";

interface Props {
  interest: Interest;
  onInterestChange: (interest: Interest) => void;
}

type Status = "idle" | "submitting" | "success" | "error";
type Fields = Omit<InquiryPayload, "interest">;
type Errors = Partial<Record<"name" | "email" | "message", string>>;

const EMPTY: Fields = { name: "", email: "", company: "", phone: "", message: "", website: "" };
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function validate(fields: Fields): Errors {
  const errors: Errors = {};
  if (!fields.name.trim()) errors.name = "Please enter your name.";
  if (!EMAIL_PATTERN.test(fields.email.trim())) errors.email = "Please enter a valid email address.";
  if (fields.message.trim().length < 10) errors.message = "Please tell us a little more (at least 10 characters).";
  return errors;
}

function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <p id={id} className="mt-1.5 text-sm text-rose-300">
      {message}
    </p>
  );
}

export default function ContactForm({ interest, onInterestChange }: Props) {
  const [fields, setFields] = useState<Fields>(EMPTY);
  const [errors, setErrors] = useState<Errors>({});
  const [status, setStatus] = useState<Status>("idle");
  const [serverError, setServerError] = useState("");

  const update = (key: keyof Fields) => (event: { target: { value: string } }) =>
    setFields((current) => ({ ...current, [key]: event.target.value }));

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const found = validate(fields);
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    setStatus("submitting");
    setServerError("");
    try {
      await submitInquiry({ ...fields, interest });
      setStatus("success");
      setFields(EMPTY);
    } catch (error) {
      setServerError(error instanceof Error ? error.message : "Something went wrong. Please try again.");
      setStatus("error");
    }
  };

  return (
    <Section
      id="contact"
      eyebrow="Contact"
      title="Let us talk about your hiring plans"
      intro="Tell us what you are looking for and we will get back to you within a few working days."
    >
      <div className="glass mx-auto max-w-2xl rounded-3xl p-6 shadow-card sm:p-10">
        {status === "success" ? (
          <div role="status" className="flex flex-col items-center gap-4 py-8 text-center">
            <CheckCircle2 aria-hidden="true" className="h-12 w-12 text-cyan-glow" />
            <h3 className="text-2xl font-semibold text-white">Thank you, we have your message.</h3>
            <p className="text-slate-300">Our team will reach out to you soon.</p>
            <button type="button" className="btn-ghost mt-2" onClick={() => setStatus("idle")}>
              Send another message
            </button>
          </div>
        ) : (
          <form onSubmit={onSubmit} noValidate className="grid gap-5 sm:grid-cols-2">
            <div>
              <label htmlFor="name" className="mb-1.5 block text-sm font-medium text-slate-200">
                Your name <span aria-hidden="true">*</span>
              </label>
              <input
                id="name"
                name="name"
                type="text"
                autoComplete="name"
                required
                maxLength={120}
                value={fields.name}
                onChange={update("name")}
                aria-invalid={Boolean(errors.name)}
                aria-describedby={errors.name ? "name-error" : undefined}
                className="field"
              />
              <FieldError id="name-error" message={errors.name} />
            </div>
            <div>
              <label htmlFor="email" className="mb-1.5 block text-sm font-medium text-slate-200">
                Work email <span aria-hidden="true">*</span>
              </label>
              <input
                id="email"
                name="email"
                type="email"
                autoComplete="email"
                required
                maxLength={254}
                value={fields.email}
                onChange={update("email")}
                aria-invalid={Boolean(errors.email)}
                aria-describedby={errors.email ? "email-error" : undefined}
                className="field"
              />
              <FieldError id="email-error" message={errors.email} />
            </div>
            <div>
              <label htmlFor="company" className="mb-1.5 block text-sm font-medium text-slate-200">
                Company
              </label>
              <input
                id="company"
                name="company"
                type="text"
                autoComplete="organization"
                maxLength={160}
                value={fields.company}
                onChange={update("company")}
                className="field"
              />
            </div>
            <div>
              <label htmlFor="phone" className="mb-1.5 block text-sm font-medium text-slate-200">
                Phone (optional)
              </label>
              <input
                id="phone"
                name="phone"
                type="tel"
                autoComplete="tel"
                maxLength={40}
                value={fields.phone}
                onChange={update("phone")}
                className="field"
              />
            </div>
            <div className="sm:col-span-2">
              <label htmlFor="interest" className="mb-1.5 block text-sm font-medium text-slate-200">
                I am interested in
              </label>
              <select
                id="interest"
                name="interest"
                value={interest}
                onChange={(event) => onInterestChange(event.target.value as Interest)}
                className="field"
              >
                {INTERESTS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="sm:col-span-2">
              <label htmlFor="message" className="mb-1.5 block text-sm font-medium text-slate-200">
                Message <span aria-hidden="true">*</span>
              </label>
              <textarea
                id="message"
                name="message"
                rows={5}
                required
                maxLength={2000}
                value={fields.message}
                onChange={update("message")}
                aria-invalid={Boolean(errors.message)}
                aria-describedby={errors.message ? "message-error" : undefined}
                className="field resize-y"
              />
              <FieldError id="message-error" message={errors.message} />
            </div>

            {/* Honeypot: invisible to people, tempting to bots. */}
            <div aria-hidden="true" className="absolute -left-[9999px] h-0 w-0 overflow-hidden">
              <label htmlFor="website">Leave this field empty</label>
              <input
                id="website"
                name="website"
                type="text"
                tabIndex={-1}
                autoComplete="off"
                value={fields.website}
                onChange={update("website")}
              />
            </div>

            <div className="sm:col-span-2">
              <button type="submit" disabled={status === "submitting"} className="btn-primary w-full disabled:opacity-70">
                {status === "submitting" ? (
                  <>
                    <Loader2 aria-hidden="true" className="h-5 w-5 animate-spin motion-reduce:animate-none" />
                    Sending...
                  </>
                ) : (
                  "Send message"
                )}
              </button>
              <p role="alert" className="mt-3 min-h-[1.5rem] text-center text-sm text-rose-300">
                {status === "error" ? serverError : ""}
              </p>
            </div>
          </form>
        )}
      </div>
    </Section>
  );
}
