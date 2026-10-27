import { Button, Result, Spin } from "antd";
import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "../../hooks/use-auth";

type RouteGuardProps = {
  children: ReactNode;
};

export function ProtectedRoute({ children }: RouteGuardProps) {
  const location = useLocation();
  const auth = useAuth();

  if (auth.isChecking) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "100vh",
          background: "var(--ui-surface,#0a0a0a)",
        }}
      >
        <Spin size="large" tip="正在验证登录状态..." />
      </div>
    );
  }

  if (!auth.isAuthenticated && auth.error) {
    return <Result status="warning" title="暂时无法验证登录状态" subTitle={auth.error}
      extra={<Button type="primary" onClick={() => window.location.reload()}>重试</Button>} />;
  }

  if (!auth.isAuthenticated && window.location.port === '9450') {
    window.location.replace('https://d.58nft.net:9461/platform-select?open=xhs');
    return null;
  }
  if (!auth.isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return <>{children}</>;
}

export function PublicOnlyRoute({ children }: RouteGuardProps) {
  const auth = useAuth();
  const location = useLocation();

  if (auth.isChecking) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "100vh",
          background: "var(--ui-surface,#0a0a0a)",
        }}
      >
        <Spin size="large" tip="正在验证登录状态..." />
      </div>
    );
  }

  if (auth.isAuthenticated) {
    const from = (location.state as { from?: { pathname?: string; search?: string } } | null)?.from;
    const target = from?.pathname?.startsWith('/') && !from.pathname.startsWith('//') ? from.pathname + (from.search || '') : '/platform-select';
    return <Navigate to={target} replace />;
  }

  return <>{children}</>;
}
