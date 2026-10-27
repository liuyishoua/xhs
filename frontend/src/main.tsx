import React from "react";
import ReactDOM from "react-dom/client";

import { AppProviders } from "./app/providers";
import { AppRouter } from "./app/router";
import "./global.css";

class AppErrorBoundary extends React.Component<{ children: React.ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return (
        <div role="alert" style={{ minHeight: "100vh", display: "grid", placeContent: "center", gap: 16, padding: 24 }}>
          <p>工作台加载失败，请重新加载页面。</p>
          <button onClick={() => window.location.reload()}>重新加载</button>
        </div>
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <AppErrorBoundary>
    <AppProviders>
      <AppRouter />
    </AppProviders>
    </AppErrorBoundary>
  </React.StrictMode>
);
