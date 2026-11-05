export type Citation = {
  chunk_id: string;
  section_id: string;
  path: string;
  excerpt: string;
  revision?: string;
};
export type Investigation = {
  answer: {
    text: string;
    refused: boolean;
    reason: string;
    citations: Citation[];
    uncertainty: string[];
  };
  steps: number;
  cost_usd: number;
  cost_kind: string;
};
export type Session = {
  workspace_id: string;
  actor_id: string;
  scopes: string[];
  providers: string[];
  console_url: string;
  demo: boolean;
  embedding: string;
};
export type Draft = {
  status: string;
  reason?: string;
  draft?: {
    action_id: string;
    payload: Record<string, unknown>;
    draft_sha256: string;
    expires_at: string;
  };
};
export type Send = <T>(
  path: string,
  data?: unknown,
  method?: string,
) => Promise<T>;
