export type Interest = "hire" | "partner" | "internships" | "other";

export interface InquiryPayload {
  name: string;
  email: string;
  company: string;
  phone: string;
  interest: Interest;
  message: string;
  website: string;
}
