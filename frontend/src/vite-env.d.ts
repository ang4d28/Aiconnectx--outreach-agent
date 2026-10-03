/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_STAT_COMPANIES?: string;
  readonly VITE_STAT_STUDENTS?: string;
  readonly VITE_STAT_MOUS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
