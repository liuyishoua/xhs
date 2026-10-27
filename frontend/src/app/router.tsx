import { Suspense, useEffect } from "react";
import { Spin } from "antd";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { pages } from "./lazy-pages";
import { AppShell } from "../components/layout/app-shell";
import { ComingSoonPage } from "../components/platforms/coming-soon";
import { ProtectedRoute } from "../components/ui/protected-route";
const ModelConfigPage = pages.ModelConfigPage.Component;
const CommentsPage = pages.CommentsPage.Component;
const ProactivePage = pages.ProactivePage.Component;
const DirectMessagesPage = pages.DirectMessagesPage.Component;
const XhsChatPage = pages.XhsChatPage.Component;
const TaskCenterPage = pages.TaskCenterPage.Component;
const AutoOpsPage = pages.AutoOpsPage.Component;
const XhsAccountsPage = pages.XhsAccountsPage.Component;
const XhsAnalyticsPage = pages.XhsAnalyticsPage.Component;
const XhsBenchmarksPage = pages.XhsBenchmarksPage.Component;
const XhsDashboard = pages.XhsDashboard.Component;
const XhsCrawlerPage = pages.XhsCrawlerPage.Component;
const XhsDiscoveryPage = pages.XhsDiscoveryPage.Component;
const XhsDraftsPage = pages.XhsDraftsPage.Component;
const XhsKeywordsPage = pages.XhsKeywordsPage.Component;
const XhsLibraryPage = pages.XhsLibraryPage.Component;
const XhsMonitoringPage = pages.XhsMonitoringPage.Component;
const XhsImageStudioPage = pages.XhsImageStudioPage.Component;
const XhsPublishPage = pages.XhsPublishPage.Component;
const XhsVideoStudioPage = pages.XhsVideoStudioPage.Component;
const XhsSectionPage = pages.XhsSectionPage.Component;

function HubRedirect({ login = false }: { login?: boolean }) {
  useEffect(() => { window.location.replace('https://d.58nft.net:9461/platform-select' + (login ? '?open=xhs' : window.location.search)); }, [login]);
  return null;
}

function PageTitle() {
  const { pathname } = useLocation();
  useEffect(() => {
    document.title = pathname.startsWith("/platforms/xhs")
      ? "小红书工作台"
      : pathname === "/login"
        ? "登录 · 运营应用中心"
        : pathname === "/models"
          ? "模型配置 · 运营应用中心"
          : pathname === "/tasks"
            ? "任务中心 · 运营应用中心"
            : "运营应用中心";
  }, [pathname]);
  return null;
}

export function AppRouter() {
  return (
    <BrowserRouter>
      <PageTitle />
      <Suspense fallback={<div style={{ padding: 32 }}><Spin tip="正在加载页面" /></div>}>
      <Routes>
        <Route path="/" element={<Navigate to="/platforms/xhs/dashboard" replace />} />
        <Route
          path="/login"
          element={
            <HubRedirect login />
          }
        />
        <Route
          path="/platform-select"
          element={
            <HubRedirect />
          }
        />
        <Route
          path="/platforms/:platformId"
          element={
            <ProtectedRoute>
              <ComingSoonPage />
            </ProtectedRoute>
          }
        />
        <Route
          element={
            <ProtectedRoute>
              <AppShell />
            </ProtectedRoute>
          }
        >
          <Route path="/platforms/xhs/comments" element={<CommentsPage />} />
          <Route path="/platforms/xhs/proactive" element={<ProactivePage />} />
          <Route path="/platforms/xhs/messages" element={<DirectMessagesPage />} />
          <Route path="/platforms/xhs/chat" element={<XhsChatPage />} />
          <Route path="/platforms/xhs/dashboard" element={<XhsDashboard />} />
          <Route path="/platforms/xhs/accounts" element={<XhsAccountsPage />} />
          <Route path="/platforms/xhs/analytics" element={<XhsAnalyticsPage />} />
          <Route path="/platforms/xhs/discovery" element={<XhsDiscoveryPage />} />
          <Route path="/platforms/xhs/crawler" element={<XhsCrawlerPage />} />
          <Route path="/platforms/xhs/keywords" element={<XhsKeywordsPage />} />
          <Route path="/platforms/xhs/library" element={<XhsLibraryPage />} />
          <Route path="/platforms/xhs/drafts" element={<XhsDraftsPage />} />
          <Route path="/platforms/xhs/benchmarks" element={<XhsBenchmarksPage />} />
          <Route path="/platforms/xhs/image-studio" element={<XhsImageStudioPage />} />
          <Route path="/platforms/xhs/video-studio" element={<XhsVideoStudioPage />} />
          <Route path="/platforms/xhs/publish" element={<XhsPublishPage />} />
          <Route path="/platforms/xhs/auto-ops" element={<AutoOpsPage />} />
          <Route path="/platforms/xhs/:section" element={<XhsSectionPage />} />
          <Route path="/tasks" element={<TaskCenterPage />} />
          <Route path="/models" element={<ModelConfigPage />} />
          <Route path="/settings" element={<Navigate to="/models" replace />} />
        </Route>
      </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
