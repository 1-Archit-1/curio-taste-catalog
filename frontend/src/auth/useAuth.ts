import { createContext, useContext } from "react";
import type { components } from "../api/schema";

export type UserSummary = components["schemas"]["UserSummaryResponse"];

export interface AuthContextValue {
  user: UserSummary | null;
  loading: boolean;
  login: (
    token: string,
  ) => Promise<{ ok: true } | { ok: false; detail: string }>;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
