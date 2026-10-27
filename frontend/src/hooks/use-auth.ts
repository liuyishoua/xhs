import axios from "axios";
import { useEffect, useSyncExternalStore } from "react";

import * as api from "../lib/api";
import type { PlatformUser } from "../types";

type AuthStatus = "checking" | "authenticated" | "anonymous";

type AuthSnapshot = {
  status: AuthStatus;
  user: PlatformUser | null;
  error: string | null;
};

let snapshot: AuthSnapshot = {
  status: "checking",
  user: null,
  error: null
};

let bootstrapped = false;
const listeners = new Set<() => void>();

function emit(next: AuthSnapshot): void {
  snapshot = next;
  listeners.forEach((listener) => listener());
}

window.addEventListener('xhs-auth-cleared', () => {
  emit({ status: 'anonymous', user: null, error: null });
});
window.addEventListener('storage', event => {
  if (event.key === 'spider_xhs_refresh_token' || event.key === null) {
    // Rotating the same user's refresh token is not an account switch.
    void recheckIdentity();
  }
});
window.addEventListener('pageshow', event => {
  if (event.persisted) window.location.reload();
});

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): AuthSnapshot {
  return snapshot;
}

function authErrorMessage(error: unknown, fallback: string): string {
  if (!axios.isAxiosError(error)) {
    return fallback;
  }

  const detail = error.response?.data?.detail;
  if (detail === "Invalid username or password") {
    return "账号不存在或密码错误，请检查后重试。";
  }
  if (detail === "Username already exists") {
    return "该平台账号已存在，请直接登录或换一个账号。";
  }
  if (typeof detail === "string" && detail.trim()) {
    return detail;
  }
  return fallback;
}

async function bootstrap(): Promise<void> {
  if (bootstrapped) {
    return;
  }
  bootstrapped = true;

  emit({ ...snapshot, status: "checking", error: null });
  try {
    const user = await api.bootstrapAuth();
    emit(user ? { status: "authenticated", user, error: null } : { status: "anonymous", user: null, error: null });
  } catch {
    emit({ status: "anonymous", user: null, error: "登录服务暂时不可用，请重试。" });
  }
}

async function login(credentials: api.AuthCredentials): Promise<void> {
  emit({ ...snapshot, error: null });
  try {
    const payload = await api.login(credentials);
    emit({ status: "authenticated", user: payload.user, error: null });
  } catch (error) {
    const message = authErrorMessage(error, "账号不存在或密码错误，请检查后重试。");
    emit({ status: "anonymous", user: null, error: message });
    throw new Error(message);
  }
}

async function register(credentials: api.AuthCredentials): Promise<void> {
  emit({ ...snapshot, error: null });
  try {
    const payload = await api.register(credentials);
    emit({ status: "authenticated", user: payload.user, error: null });
  } catch (error) {
    const message = authErrorMessage(error, "注册失败，该平台账号可能已存在。");
    emit({ status: "anonymous", user: null, error: message });
    throw new Error(message);
  }
}

async function logout(): Promise<void> {
  try { await api.logout(); }
  catch { emit({ ...snapshot, error: '退出未完成，请稍后重试。' }); }
}

export function useAuth() {
  const current = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    void bootstrap();
  }, []);

  return {
    ...current,
    isAuthenticated: current.status === "authenticated",
    isChecking: current.status === "checking",
    login,
    register,
    logout
  };
}

// Read the shared identity without persisting another refresh token: otherwise
// two tabs would keep waking and redirecting one another indefinitely.
let identityCheck: Promise<void> | null = null;
function recheckIdentity(): Promise<void> {
  if (snapshot.status !== 'authenticated') return Promise.resolve();
  if (identityCheck) return identityCheck;
  const owner = snapshot.user?.id;
  identityCheck = api.exchangeWorkspace(false).then(payload => {
    if (payload.user?.id !== owner) {
      // Reload this exact route; bootstrap replaces tokens before rendering.
      // Emitting anonymous here would send the route guard through the hub.
      window.location.reload();
    }
  }).catch(error => {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      api.clearAuthTokens(); // The route guard performs the single redirect.
    }
  }).finally(() => { identityCheck = null; });
  return identityCheck;
}
window.addEventListener('focus', () => { void recheckIdentity(); });
