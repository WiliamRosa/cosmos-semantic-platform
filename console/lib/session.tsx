"use client";

// The credential and data loading. A 401 from any view opens the sign-in form; saving a
// credential bumps `version`, and every view reloads.

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { ApiError, getCredential, setCredential } from "./api";

interface Session {
  version: number;
  needsAuth: boolean;
  hasCredential: boolean;
  requireAuth: () => void;
  signIn: (credential: string) => void;
  signOut: () => void;
}

const Context = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [version, setVersion] = useState(0);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [hasCredential, setHasCredential] = useState(false);

  useEffect(() => setHasCredential(Boolean(getCredential())), [version]);

  const requireAuth = useCallback(() => setNeedsAuth(true), []);
  const signIn = useCallback((credential: string) => {
    setCredential(credential.trim());
    setNeedsAuth(false);
    setVersion((v) => v + 1);
  }, []);
  const signOut = useCallback(() => {
    setCredential("");
    setVersion((v) => v + 1);
    setNeedsAuth(true);
  }, []);

  return (
    <Context.Provider value={{ version, needsAuth, hasCredential, requireAuth, signIn, signOut }}>
      {children}
    </Context.Provider>
  );
}

export function useSession(): Session {
  const value = useContext(Context);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}

export interface Loaded<T> {
  data: T | null;
  error: ApiError | Error | null;
  loading: boolean;
  reload: () => void;
}

export function useLoad<T>(load: () => Promise<T>, key: string = ""): Loaded<T> {
  const { version, requireAuth } = useSession();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    load()
      .then((value) => alive && setData(value))
      .catch((err: Error) => {
        if (!alive) return;
        if (err instanceof ApiError && err.status === 401) requireAuth();
        setError(err);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // `load` changes identity on every render; `key` names what it loads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, version, tick, requireAuth]);

  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}
