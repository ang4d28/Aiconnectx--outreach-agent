import { lazy } from "react";

/** Each page is its own chunk; the dashboard shell (Layout) renders without waiting for any of them. */
export const loaders = {
  Overview: () => import("./Overview"),
  Leads: () => import("./Leads"),
  Drafts: () => import("./Drafts"),
  Replies: () => import("./Replies"),
  Attention: () => import("./Attention"),
  Inquiries: () => import("./Inquiries"),
  Settings: () => import("./Settings"),
  Users: () => import("./Users"),
};

export const Pages = {
  Overview: lazy(loaders.Overview),
  Leads: lazy(loaders.Leads),
  Drafts: lazy(loaders.Drafts),
  Replies: lazy(loaders.Replies),
  Attention: lazy(loaders.Attention),
  Inquiries: lazy(loaders.Inquiries),
  Settings: lazy(loaders.Settings),
  Users: lazy(loaders.Users),
};

/** After the first screen is up, fetch the other page chunks in the background so tab clicks are instant. */
export function preloadPages(names: (keyof typeof loaders)[]): void {
  for (const name of names) void loaders[name]().catch(() => undefined);
}
