import { lazy } from "react";
import type { ComponentType } from "react";

function definePage<T extends ComponentType>(loader: () => Promise<{ default: T }>) {
  let pending: Promise<{ default: T }> | undefined;
  const load = () => pending ?? (pending = loader().catch((error) => {
    pending = undefined;
    throw error;
  }));
  return { Component: lazy(load), preload: load };
}

export const pages = {
  ProactivePage: definePage(() => import("../pages/platforms/xhs/proactive-page").then((module) => ({ default: module.ProactivePage }))),
  ModelConfigPage: definePage(() => import("../pages/models/model-config-page").then((module) => ({ default: module.ModelConfigPage }))),
  CommentsPage: definePage(() => import("../pages/platforms/xhs/comments-page").then((module) => ({ default: module.CommentsPage }))),
  DirectMessagesPage: definePage(() => import("../pages/platforms/xhs/direct-messages-page").then((module) => ({ default: module.DirectMessagesPage }))),
  XhsChatPage: definePage(() => import("../pages/platforms/xhs/chat-page").then((module) => ({ default: module.XhsChatPage }))),
  TaskCenterPage: definePage(() => import("../pages/tasks/task-center-page").then((module) => ({ default: module.TaskCenterPage }))),
  AutoOpsPage: definePage(() => import("../pages/platforms/xhs/auto-ops-page").then((module) => ({ default: module.AutoOpsPage }))),
  XhsAccountsPage: definePage(() => import("../pages/platforms/xhs/accounts-page").then((module) => ({ default: module.XhsAccountsPage }))),
  XhsAnalyticsPage: definePage(() => import("../pages/platforms/xhs/analytics-page").then((module) => ({ default: module.XhsAnalyticsPage }))),
  XhsBenchmarksPage: definePage(() => import("../pages/platforms/xhs/benchmarks-page").then((module) => ({ default: module.XhsBenchmarksPage }))),
  XhsDashboard: definePage(() => import("../pages/platforms/xhs/xhs-dashboard").then((module) => ({ default: module.XhsDashboard }))),
  XhsCrawlerPage: definePage(() => import("../pages/platforms/xhs/crawler-page").then((module) => ({ default: module.XhsCrawlerPage }))),
  XhsDiscoveryPage: definePage(() => import("../pages/platforms/xhs/discovery-page").then((module) => ({ default: module.XhsDiscoveryPage }))),
  XhsDraftsPage: definePage(() => import("../pages/platforms/xhs/rewrite-page").then((module) => ({ default: module.XhsDraftsPage }))),
  XhsKeywordsPage: definePage(() => import("../pages/platforms/xhs/keywords-page").then((module) => ({ default: module.XhsKeywordsPage }))),
  XhsLibraryPage: definePage(() => import("../pages/platforms/xhs/library-page").then((module) => ({ default: module.XhsLibraryPage }))),
  XhsMonitoringPage: definePage(() => import("../pages/platforms/xhs/monitoring-page").then((module) => ({ default: module.XhsMonitoringPage }))),
  XhsImageStudioPage: definePage(() => import("../pages/platforms/xhs/image-studio-page").then((module) => ({ default: module.XhsImageStudioPage }))),
  XhsPublishPage: definePage(() => import("../pages/platforms/xhs/publish-page").then((module) => ({ default: module.XhsPublishPage }))),
  XhsVideoStudioPage: definePage(() => import("../pages/platforms/xhs/video-studio-page").then((module) => ({ default: module.XhsVideoStudioPage }))),
  XhsSectionPage: definePage(() => import("../pages/platforms/xhs/xhs-section-page").then((module) => ({ default: module.XhsSectionPage }))),
};

const routePages: Record<string, keyof typeof pages> = {
  "/platforms/xhs/proactive": "ProactivePage",
  "/platforms/xhs/comments": "CommentsPage",
  "/platforms/xhs/messages": "DirectMessagesPage",
  "/platforms/xhs/chat": "XhsChatPage",
  "/platforms/xhs/dashboard": "XhsDashboard",
  "/platforms/xhs/accounts": "XhsAccountsPage",
  "/platforms/xhs/analytics": "XhsAnalyticsPage",
  "/platforms/xhs/discovery": "XhsDiscoveryPage",
  "/platforms/xhs/crawler": "XhsCrawlerPage",
  "/platforms/xhs/keywords": "XhsKeywordsPage",
  "/platforms/xhs/library": "XhsLibraryPage",
  "/platforms/xhs/drafts": "XhsDraftsPage",
  "/platforms/xhs/benchmarks": "XhsBenchmarksPage",
  "/platforms/xhs/image-studio": "XhsImageStudioPage",
  "/platforms/xhs/video-studio": "XhsVideoStudioPage",
  "/platforms/xhs/publish": "XhsPublishPage",
  "/platforms/xhs/auto-ops": "AutoOpsPage",
  "/tasks": "TaskCenterPage",
  "/models": "ModelConfigPage",
};

export function preloadRoute(path: string): void {
  const page = routePages[path.split("?")[0]];
  if (page) void pages[page].preload().catch(() => {});
}
