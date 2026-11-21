import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { apiClient, extractErrorDetail, onUnauthorized } from "../api/client";
import { AuthContext, type UserSummary } from "./useAuth";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserSummary | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;

    const unsubscribe = onUnauthorized(() => {
      if (active) {
        setUser(null);
      }
    });

    (async () => {
      try {
        const { data, response } = await apiClient.GET("/api/auth/me");
        if (!active) return;
        if (response.ok && data) {
          setUser(data);
        } else {
          setUser(null);
        }
      } catch {
        if (active) setUser(null);
      } finally {
        if (active) setLoading(false);
      }
    })();

    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  const login = useCallback(async (token: string) => {
    const { data, error, response } = await apiClient.POST("/api/auth/login", {
      body: { token },
    });
    if (response.ok && data) {
      setUser(data.user);
      return { ok: true as const };
    }
    return {
      ok: false as const,
      detail: extractErrorDetail(error, "Invalid login token."),
    };
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiClient.POST("/api/auth/logout");
    } finally {
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, logout }),
    [user, loading, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
