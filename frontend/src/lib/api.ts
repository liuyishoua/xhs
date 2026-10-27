import axios from "axios";
import { message } from "antd";

import { fallbackPlatforms } from "./platforms";
import type {
  AuthPayload,
  AutoTask,
  AutoTaskCreatePayload,
  AutoTaskRunResult,
  AutoTaskUpdatePayload,
  AnalyticsCommentInsight,
  AnalyticsHotTopic,
  AnalyticsReportPayload,
  AnalyticsReportResponse,
  AnalyticsTopContent,
  AppNotification,
  BenchmarkCreateDraftsResponse,
  BenchmarkOverview,
  BatchCreateDraftsPayload,
  BatchCreateDraftsResponse,
  BatchTagNotesPayload,
  BatchTagNotesResponse,
  ComposeImagePayload,
  CreateDraftPayload,
  DashboardOverview,
  Draft,
  DescribeImagePayload,
  GenerateNotePayload,
  GenerateCoverPayload,
  GenerateImagePayload,
  GenerateImageResult,
  GeneratedImageAsset,
  GenerateTagsPayload,
  GenerateTitlePayload,
  ImageUtilityFile,
  KeywordGroup,
  KeywordGroupDetail,
  KeywordGroupPayload,
  ModelConfig,
  ModelConfigPayload,
  ModelType,
  MonitoringNote,
  MonitoringRefreshResponse,
  MonitoringSnapshot,
  MonitoringTarget,
  MonitoringTargetPayload,
  NoteAsset,
  NoteComment,
  NotesExportPayload,
  NotesExportResponse,
  Paginated,
  PlatformAccount,
  PlatformMeta,
  PlatformUser,
  PublishAsset,
  PublishAssetPayload,
  PublishJob,
  PublishJobUpdatePayload,
  PolishTextPayload,
  ResizeImagePayload,
  RewriteDraftPayload,
  RunDueTasksResponse,
  SchedulerStatus,
  SavedNote,
  SaveNotesResponse,
  SendDraftToPublishPayload,
  Tag,
  UserImageFile,
  TagPayload,
  TaskRecord,
  XhsNoteSearchResponse,
  XhsDataCrawlItem,
  XhsDataCrawlPayload,
  XhsDataCrawlResponse,
  XhsSearchOptions,
  XhsSearchNote,
  XhsQrLoginSession
} from "../types";

const http = axios.create({
  baseURL: "/api",
  timeout: 120000,
});

const REFRESH_TOKEN_KEY = "spider_xhs_refresh_token";
let accessToken: string | null = null;
let authEpoch = 0;
let workspaceUserId: number | null = null;
type SessionRequest = { _authEpoch?: number; _authRetry?: boolean; _silent?: boolean };

export type AuthCredentials = {
  username: string;
  password: string;
};

export function getAccessToken(): string | null {
  return accessToken;
}

export function hasRefreshToken(): boolean {
  return Boolean(window.localStorage.getItem(REFRESH_TOKEN_KEY));
}

function getRefreshToken(): string | null {
  return window.localStorage.getItem(REFRESH_TOKEN_KEY);
}

function setAccessToken(token: string | null): void {
  accessToken = token;
}

function persistAuthPayload(payload: AuthPayload): AuthPayload {
  workspaceUserId = payload.user?.id ?? null;
  setAccessToken(payload.access_token);
  if (payload.refresh_token) {
    window.localStorage.setItem(REFRESH_TOKEN_KEY, payload.refresh_token);
  }
  return payload;
}

export function clearAuthTokens(): void {
  authEpoch += 1;
  setAccessToken(null);
  workspaceUserId = null;
  window.localStorage.removeItem(REFRESH_TOKEN_KEY);
  window.dispatchEvent(new Event('xhs-auth-cleared'));
}

http.interceptors.request.use((config) => {
  (config as typeof config & SessionRequest)._authEpoch = authEpoch;
  if (accessToken) {
    config.headers.Authorization = `Bearer ${accessToken}`;
  }
  return config;
});

http.interceptors.response.use(
  (response) => {
    if ((response.config as typeof response.config & SessionRequest)._authEpoch !== authEpoch) {
      throw new axios.CanceledError('登录用户已变化，丢弃旧请求结果');
    }
    if (response.config.method !== 'get' && !response.config.url?.startsWith('/auth/')) {
      window.dispatchEvent(new Event('xhs-workspace-data-changed'));
    }
    return response;
  },
  async (error) => {
    if (axios.isCancel(error)) {
      return Promise.reject(error);
    }
    const originalRequest = error.config as typeof error.config & SessionRequest;
    if (originalRequest?._authEpoch !== undefined && originalRequest._authEpoch !== authEpoch) {
      return Promise.reject(new axios.CanceledError('登录用户已变化，丢弃旧请求结果'));
    }
    // Authentication endpoints must fail directly: refreshing a rejected
    // refresh token here would recursively refresh forever.
    if (/^\/auth\/(login|register|refresh|workspace\/exchange)$/.test(originalRequest?.url || '')) {
      return Promise.reject(error);
    }
    if (error.response?.status !== 401 || originalRequest?._authRetry || !getRefreshToken()) {
      if (!originalRequest?._silent) {
        const detail = error.response?.data?.detail;
        const msg = typeof detail === "string" ? detail : "请求失败，请稍后重试";
        message.error(msg);
      }
      return Promise.reject(error);
    }

    originalRequest._authRetry = true;
    try {
      const token = await refreshAccessToken();
      originalRequest.headers.Authorization = `Bearer ${token}`;
      return http(originalRequest);
    } catch (refreshError) {
      clearAuthTokens();
      message.error("登录已过期，请重新登录");
      return Promise.reject(refreshError);
    }
  }
);

export async function login(credentials: AuthCredentials): Promise<AuthPayload> {
  clearAuthTokens();
  const response = await http.post<AuthPayload>("/auth/login", credentials);
  return persistAuthPayload(response.data);
}

export async function register(credentials: AuthCredentials): Promise<AuthPayload> {
  clearAuthTokens();
  const response = await http.post<AuthPayload>("/auth/register", credentials);
  return persistAuthPayload(response.data);
}

export async function refreshAccessToken(): Promise<string> {
  const epoch = authEpoch;
  const refreshToken = getRefreshToken();
  if (!refreshToken) {
    throw new Error("Missing refresh token");
  }

  const response = await http.post<{ access_token: string; token_type: "bearer" }>("/auth/refresh", {
    refresh_token: refreshToken
  });
  if (authEpoch !== epoch || getRefreshToken() !== refreshToken) {
    throw new axios.CanceledError('登录用户已变化');
  }
  setAccessToken(response.data.access_token);
  return response.data.access_token;
}

export async function fetchMe(): Promise<PlatformUser> {
  const response = await http.get<PlatformUser>("/auth/me");
  workspaceUserId = response.data.id;
  await http.post("/auth/workspace/session");
  return response.data;
}

export async function exchangeWorkspace(persist = true): Promise<AuthPayload> {
  const response = await http.post<AuthPayload>("/auth/workspace/exchange", {}, {
    headers: { "X-App-Request": "1" }, timeout: 10000, ...{ _silent: true }
  });
  return persist ? persistAuthPayload(response.data) : response.data;
}

export async function bootstrapAuth(): Promise<PlatformUser | null> {
  try {
    const payload = await exchangeWorkspace();
    return payload.user ?? null;
  } catch (error) {
    if (!axios.isAxiosError(error) || error.response?.status !== 401) throw error;
    if (['9450', '9461'].includes(window.location.port)) {
      clearAuthTokens();
      return null;
    }
  }
  if (!getAccessToken() && hasRefreshToken()) {
    await refreshAccessToken();
  }
  if (!getAccessToken()) {
    return null;
  }
  return fetchMe();
}

export async function logout(): Promise<void> {
  const token = accessToken;
  await http.post("/auth/logout", undefined, { headers: token ? { Authorization: `Bearer ${token}` } : {}, timeout: 10000 });
  clearAuthTokens();
}

export async function fetchPlatforms(): Promise<PlatformMeta[]> {
  try {
    const response = await http.get<Paginated<PlatformMeta>>("/platforms");
    return response.data.items;
  } catch {
    return fallbackPlatforms;
  }
}

export async function fetchXhsOverview(): Promise<DashboardOverview> {
  const response = await http.get<DashboardOverview>("/xhs/analytics/overview");
  return response.data;
}

export async function fetchXhsTopContent(): Promise<{ items: AnalyticsTopContent[] }> {
  const response = await http.get<{ items: AnalyticsTopContent[] }>("/xhs/analytics/top-content");
  return response.data;
}

export async function fetchXhsHotTopics(): Promise<{ items: AnalyticsHotTopic[] }> {
  const response = await http.get<{ items: AnalyticsHotTopic[] }>("/xhs/analytics/hot-topics");
  return response.data;
}

export async function fetchXhsCommentInsights(): Promise<AnalyticsCommentInsight> {
  const response = await http.get<AnalyticsCommentInsight>("/xhs/analytics/comment-insights");
  return response.data;
}

export async function fetchXhsBenchmarks(): Promise<BenchmarkOverview> {
  const response = await http.get<BenchmarkOverview>("/xhs/analytics/benchmarks");
  return response.data;
}

export async function createBenchmarkDrafts(targetId: number, limit = 5): Promise<BenchmarkCreateDraftsResponse> {
  const response = await http.post<BenchmarkCreateDraftsResponse>(
    `/xhs/analytics/benchmarks/${targetId}/create-drafts`,
    null,
    { params: { limit } }
  );
  return response.data;
}

export async function createXhsAnalyticsReport(
  payload: AnalyticsReportPayload = { format: "json" }
): Promise<AnalyticsReportResponse> {
  const response = await http.post<AnalyticsReportResponse>("/xhs/analytics/reports", payload);
  return response.data;
}

export async function fetchAccounts(platform = "xhs"): Promise<PlatformAccount[]> {
  const response = await http.get<Paginated<PlatformAccount>>("/accounts", { params: { platform } });
  return response.data.items;
}

export type SavedNoteFilters = {
  platform?: string;
  q?: string;
  tag_id?: number;
  has_assets?: boolean;
  has_comments?: boolean;
  page_size?: number;
};

export async function fetchSavedNoteIds(platform = "xhs"): Promise<string[]> {
  const response = await http.get<{ items: string[] }>("/notes/ids", { params: { platform } });
  return response.data.items;
}

export async function fetchSavedNotes(platformOrFilters: string | SavedNoteFilters = "xhs"): Promise<Paginated<SavedNote>> {
  const params =
    typeof platformOrFilters === "string"
      ? { platform: platformOrFilters }
      : {
          platform: platformOrFilters.platform ?? "xhs",
          q: platformOrFilters.q || undefined,
          tag_id: platformOrFilters.tag_id,
          has_assets: platformOrFilters.has_assets,
          has_comments: platformOrFilters.has_comments,
          page_size: platformOrFilters.page_size,
        };
  const response = await http.get<Paginated<SavedNote>>("/notes", { params });
  return response.data;
}

export async function fetchSavedNote(noteId: number, silent = false): Promise<SavedNote> {
  const response = await http.get<SavedNote>(`/notes/${noteId}`, { _silent: silent } as never);
  return response.data;
}

export async function fetchSavedNoteAssets(noteId: number): Promise<Paginated<NoteAsset>> {
  const response = await http.get<Paginated<NoteAsset>>(`/notes/${noteId}/assets`);
  return response.data;
}

export async function addNoteAsset(noteId: number, payload: { asset_type: string; url?: string; local_path?: string }): Promise<NoteAsset> {
  const response = await http.post<NoteAsset>(`/notes/${noteId}/assets`, payload);
  return response.data;
}

export async function deleteNoteAsset(noteId: number, assetId: number): Promise<void> {
  await http.delete(`/notes/${noteId}/assets/${assetId}`);
}

export async function reorderNoteAssets(noteId: number, assetIds: number[]): Promise<void> {
  await http.put(`/notes/${noteId}/assets/reorder`, { asset_ids: assetIds });
}

export async function fetchSavedNoteComments(noteId: number, page = 1): Promise<Paginated<NoteComment>> {
  const response = await http.get<Paginated<NoteComment>>(`/notes/${noteId}/comments`, {
    params: { page, page_size: 50 }
  });
  return response.data;
}

export async function deleteSavedNote(noteId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/notes/${noteId}`);
  return response.data;
}

export async function fetchTags(): Promise<Paginated<Tag>> {
  const response = await http.get<Paginated<Tag>>("/tags");
  return response.data;
}

export async function createTag(payload: TagPayload): Promise<Tag> {
  const response = await http.post<Tag>("/tags", payload);
  return response.data;
}

export async function batchTagNotes(payload: BatchTagNotesPayload): Promise<BatchTagNotesResponse> {
  const response = await http.post<BatchTagNotesResponse>("/notes/batch-tag", payload);
  return response.data;
}

export async function batchCreateDraftsFromNotes(
  payload: BatchCreateDraftsPayload
): Promise<BatchCreateDraftsResponse> {
  const response = await http.post<BatchCreateDraftsResponse>("/notes/batch-create-drafts", payload);
  return response.data;
}

export async function exportSavedNotes(payload: NotesExportPayload): Promise<NotesExportResponse> {
  const response = await http.post<NotesExportResponse>("/notes/export", payload);
  return response.data;
}

export async function downloadExportFile(downloadUrl: string, fileName: string): Promise<void> {
  const endpoint = downloadUrl.startsWith("/api") ? downloadUrl.slice(4) : downloadUrl;
  const response = await http.get<Blob>(endpoint, { responseType: "blob" });
  const objectUrl = window.URL.createObjectURL(response.data);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(objectUrl);
}

export async function downloadMediaFile(downloadUrl: string, fileName: string): Promise<void> {
  return downloadExportFile(downloadUrl, fileName);
}

export type UploadedFile = {
  file_name: string;
  file_path: string;
  download_url: string;
  asset_type: "image" | "video";
  size: number;
};

export async function uploadAssetFile(file: File): Promise<UploadedFile> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await http.post<UploadedFile>("/files/upload", formData, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 120000,
  });
  return response.data;
}

export async function createMediaObjectUrl(downloadUrl: string): Promise<string> {
  const endpoint = downloadUrl.startsWith("/api") ? downloadUrl.slice(4) : downloadUrl;
  const response = await http.get<Blob>(endpoint, { responseType: "blob" });
  return window.URL.createObjectURL(response.data);
}

export async function searchXhsNotes(payload: XhsSearchOptions): Promise<XhsNoteSearchResponse> {
  const response = await http.post<XhsNoteSearchResponse>("/xhs/pc/search/notes", payload);
  return response.data;
}

export async function crawlXhsDataStream(
  payload: XhsDataCrawlPayload,
  onItem: (index: number, item: XhsDataCrawlItem) => void,
  onProgress?: (message: string) => void,
  onError?: (message: string) => void,
): Promise<{ total: number; success_count: number; failed_count: number }> {
  const token = getAccessToken();
  const response = await fetch("/api/xhs/crawl/data", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail || `HTTP ${response.status}`);
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response stream");
  const decoder = new TextDecoder();
  let buffer = "";
  let result = { total: 0, success_count: 0, failed_count: 0 };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      try {
        const event = JSON.parse(line.slice(6));
        if (event.type === "item") onItem(event.index, event.item);
        else if (event.type === "progress") onProgress?.(event.message);
        else if (event.type === "error") onError?.(event.message);
        else if (event.type === "done") result = { total: event.total, success_count: event.success_count, failed_count: event.failed_count };
      } catch { /* skip malformed events */ }
    }
  }
  return result;
}

export async function fetchXhsNoteDetail(payload: { account_id: number; url: string }): Promise<XhsSearchNote> {
  const response = await http.post<XhsSearchNote>("/xhs/pc/notes/detail", payload);
  return response.data;
}

export async function fetchXhsNoteComments(payload: {
  account_id: number;
  note_url: string;
}): Promise<Paginated<NoteComment>> {
  const response = await http.post<{ total: number; items: NoteComment[] }>("/xhs/pc/notes/comments", payload);
  return {
    total: response.data.total,
    page: 1,
    page_size: response.data.items.length,
    items: response.data.items
  };
}

export async function saveXhsNotesToLibrary(payload: {
  account_id: number;
  notes: XhsSearchNote[];
}): Promise<SaveNotesResponse> {
  const response = await http.post<SaveNotesResponse>("/notes/batch-save", payload);
  return response.data;
}

export async function createDraftFromNote(payload: CreateDraftPayload): Promise<Draft> {
  const response = await http.post<Draft>("/drafts", payload);
  return response.data;
}

export async function fetchDrafts(platform = "xhs"): Promise<Paginated<Draft>> {
  const response = await http.get<Paginated<Draft>>("/drafts", { params: { platform } });
  return response.data;
}

export async function updateDraft(draftId: number, payload: { title?: string; body?: string; tags?: { id?: string; name: string }[] }): Promise<Draft> {
  const response = await http.patch<Draft>(`/drafts/${draftId}`, payload);
  return response.data;
}

export async function deleteDraft(draftId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/drafts/${draftId}`);
  return response.data;
}

export type DraftAsset = {
  id: number;
  draft_id: number;
  asset_type: "image" | "video" | string;
  url: string;
  local_path: string;
  sort_order: number;
};

export async function fetchDraftAssets(draftId: number): Promise<{ items: DraftAsset[] }> {
  const response = await http.get<{ items: DraftAsset[] }>(`/drafts/${draftId}/assets`);
  return response.data;
}

export async function addDraftAsset(draftId: number, payload: { asset_type: string; url?: string; local_path?: string }): Promise<DraftAsset> {
  const response = await http.post<DraftAsset>(`/drafts/${draftId}/assets`, payload);
  return response.data;
}

export async function deleteDraftAsset(draftId: number, assetId: number): Promise<void> {
  await http.delete(`/drafts/${draftId}/assets/${assetId}`);
}

export async function updateDraftAsset(draftId: number, assetId: number, payload: { url?: string; local_path?: string }): Promise<DraftAsset> {
  const response = await http.patch<DraftAsset>(`/drafts/${draftId}/assets/${assetId}`, payload);
  return response.data;
}

export async function reorderDraftAssets(draftId: number, assetIds: number[]): Promise<void> {
  await http.put(`/drafts/${draftId}/assets/reorder`, { asset_ids: assetIds });
}

export async function sendDraftToPublish(draftId: number, payload: SendDraftToPublishPayload): Promise<PublishJob> {
  const response = await http.post<PublishJob>(`/drafts/${draftId}/send-to-publish`, payload);
  return response.data;
}

export function saveAccountSelection(key: string, accountId: number) {
  if (!workspaceUserId) return;
  try { localStorage.setItem(`${key}:u${workspaceUserId}`, String(accountId)); } catch { /* Optional persistence. */ }
}
export function readAccountSelection(key: string): number | undefined {
  if (!workspaceUserId) return undefined;
  try { const id = Number(localStorage.getItem(`${key}:u${workspaceUserId}`)); return id > 0 ? id : undefined; } catch { return undefined; }
}
export function setXhsAiAccount(accountId: number) {
  saveAccountSelection('xhs_ai_account_id', accountId);
}
export function getXhsAiAccount(): number | undefined {
  return readAccountSelection('xhs_ai_account_id');
}
function aiAccountPayload<T extends object>(payload: T): T & { account_id?: number } {
  return { account_id: getXhsAiAccount(), ...payload };
}

export async function rewriteDraftWithAi(payload: RewriteDraftPayload): Promise<Draft> {
  const response = await http.post<Draft>("/ai/rewrite-note", aiAccountPayload(payload));
  return response.data;
}

export async function generateNoteWithAi(payload: GenerateNotePayload): Promise<Draft> {
  const response = await http.post<Draft>("/ai/generate-note", aiAccountPayload(payload));
  return response.data;
}

export async function generateTitleOptions(payload: GenerateTitlePayload): Promise<{ items: string[] }> {
  const response = await http.post<{ items: string[] }>("/ai/generate-title", aiAccountPayload(payload));
  return response.data;
}

export async function generateTagOptions(payload: GenerateTagsPayload): Promise<{ items: string[] }> {
  const response = await http.post<{ items: string[] }>("/ai/generate-tags", aiAccountPayload(payload));
  return response.data;
}

export async function polishTextWithAi(payload: PolishTextPayload): Promise<{ text: string }> {
  const response = await http.post<{ text: string }>("/ai/polish-text", aiAccountPayload(payload));
  return response.data;
}

export async function fetchGeneratedImageAssets(): Promise<Paginated<GeneratedImageAsset>> {
  const response = await http.get<Paginated<GeneratedImageAsset>>("/ai/images/assets");
  return response.data;
}

export async function deleteGeneratedImageAsset(assetId: number): Promise<void> {
  await http.delete(`/ai/images/assets/${assetId}`);
}

export async function deleteUserImage(fileName: string): Promise<void> {
  await http.delete(`/files/images/${fileName}`);
}

export async function generateCoverWithAi(payload: GenerateCoverPayload): Promise<GeneratedImageAsset> {
  const response = await http.post<GeneratedImageAsset>("/ai/images/generate-cover", aiAccountPayload(payload));
  return response.data;
}

export async function generateImageWithAi(payload: GenerateImagePayload): Promise<GenerateImageResult> {
  const response = await http.post<GenerateImageResult>("/ai/images/generate", aiAccountPayload(payload), { timeout: 180000 });
  return response.data;
}

export async function fetchUserImages(): Promise<{ items: UserImageFile[] }> {
  const response = await http.get<{ items: UserImageFile[] }>("/files/images");
  return response.data;
}

export async function describeImageWithAi(payload: DescribeImagePayload): Promise<{ text: string }> {
  const response = await http.post<{ text: string }>("/ai/images/describe", aiAccountPayload(payload));
  return response.data;
}

export async function composeImageUtility(payload: ComposeImagePayload): Promise<ImageUtilityFile> {
  const response = await http.post<ImageUtilityFile>("/files/images/compose", payload);
  return response.data;
}

export async function resizeImageUtility(payload: ResizeImagePayload): Promise<ImageUtilityFile> {
  const response = await http.post<ImageUtilityFile>("/files/images/resize", payload);
  return response.data;
}

export async function fetchModelConfigs(modelType?: ModelType): Promise<Paginated<ModelConfig>> {
  const response = await http.get<Paginated<ModelConfig>>("/model-configs", {
    params: modelType ? { model_type: modelType } : undefined
  });
  return response.data;
}

export async function createModelConfig(payload: ModelConfigPayload): Promise<ModelConfig> {
  const response = await http.post<ModelConfig>("/model-configs", payload);
  return response.data;
}

export async function setDefaultModelConfig(configId: number): Promise<ModelConfig> {
  const response = await http.post<ModelConfig>(`/model-configs/${configId}/set-default`);
  return response.data;
}

export async function updateModelConfig(configId: number, payload: Partial<ModelConfigPayload>): Promise<ModelConfig> {
  const response = await http.patch<ModelConfig>(`/model-configs/${configId}`, payload);
  return response.data;
}

export async function testModelConfig(configId: number): Promise<{ id: number; status: string; message: string }> {
  const response = await http.post<{ id: number; status: string; message: string }>(`/model-configs/${configId}/test`);
  return response.data;
}

export async function deleteModelConfig(configId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/model-configs/${configId}`);
  return response.data;
}

export async function fetchTasks(platform?: string): Promise<Paginated<TaskRecord>> {
  const response = await http.get<Paginated<TaskRecord>>("/tasks", {
    params: platform ? { platform } : undefined
  });
  return response.data;
}

export async function fetchSchedulerStatus(): Promise<SchedulerStatus> {
  const response = await http.get<SchedulerStatus>("/tasks/scheduler/status");
  return response.data;
}

export async function cancelTask(taskId: number): Promise<TaskRecord> {
  const response = await http.post<TaskRecord>(`/tasks/${taskId}/cancel`);
  return response.data;
}

export async function retryTask(taskId: number): Promise<TaskRecord> {
  const response = await http.post<TaskRecord>(`/tasks/${taskId}/retry`);
  return response.data;
}

export async function runDueTasks(platform = "xhs"): Promise<RunDueTasksResponse> {
  const response = await http.post<RunDueTasksResponse>("/tasks/run-due", null, { params: { platform } });
  return response.data;
}

export async function fetchMonitoringTargets(): Promise<Paginated<MonitoringTarget>> {
  const response = await http.get<Paginated<MonitoringTarget>>("/xhs/monitoring/targets");
  return response.data;
}

export async function createMonitoringTarget(payload: MonitoringTargetPayload): Promise<MonitoringTarget> {
  const response = await http.post<MonitoringTarget>("/xhs/monitoring/targets", payload);
  return response.data;
}

export async function updateMonitoringTarget(
  targetId: number,
  payload: Partial<MonitoringTargetPayload>
): Promise<MonitoringTarget> {
  const response = await http.patch<MonitoringTarget>(`/xhs/monitoring/targets/${targetId}`, payload);
  return response.data;
}

export async function deleteMonitoringTarget(targetId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/xhs/monitoring/targets/${targetId}`);
  return response.data;
}

export async function refreshMonitoringTarget(targetId: number): Promise<MonitoringRefreshResponse> {
  const response = await http.post<MonitoringRefreshResponse>(`/xhs/monitoring/targets/${targetId}/refresh`);
  return response.data;
}

export async function fetchMonitoringSnapshots(targetId: number): Promise<{ target_id: number; items: MonitoringSnapshot[] }> {
  const response = await http.get<{ target_id: number; items: MonitoringSnapshot[] }>(
    `/xhs/monitoring/targets/${targetId}/snapshots`
  );
  return response.data;
}

export async function fetchMonitoringTargetNotes(targetId: number): Promise<{ target_id: number; items: MonitoringNote[] }> {
  const response = await http.get<{ target_id: number; items: MonitoringNote[] }>(
    `/xhs/monitoring/targets/${targetId}/notes`
  );
  return response.data;
}

export async function fetchKeywordGroups(platform = "xhs"): Promise<Paginated<KeywordGroup>> {
  const response = await http.get<Paginated<KeywordGroup>>("/keyword-groups", { params: { platform } });
  return response.data;
}

export async function createKeywordGroup(payload: KeywordGroupPayload): Promise<KeywordGroup> {
  const response = await http.post<KeywordGroup>("/keyword-groups", payload);
  return response.data;
}

export async function fetchKeywordGroup(groupId: number): Promise<KeywordGroupDetail> {
  const response = await http.get<KeywordGroupDetail>(`/keyword-groups/${groupId}`);
  return response.data;
}

export async function updateKeywordGroup(
  groupId: number,
  payload: Partial<KeywordGroupPayload>
): Promise<KeywordGroup> {
  const response = await http.patch<KeywordGroup>(`/keyword-groups/${groupId}`, payload);
  return response.data;
}

export async function deleteKeywordGroup(groupId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/keyword-groups/${groupId}`);
  return response.data;
}

export async function fetchPublishJobs(platform = "xhs"): Promise<Paginated<PublishJob>> {
  const response = await http.get<Paginated<PublishJob>>("/publish/jobs", { params: { platform } });
  return response.data;
}

export async function fetchPublishJob(jobId: number, signal?: AbortSignal): Promise<PublishJob> {
  const response = await http.get<PublishJob>(`/publish/jobs/${jobId}`, { signal, timeout: 10000 });
  return response.data;
}

export async function updatePublishJob(jobId: number, payload: PublishJobUpdatePayload): Promise<PublishJob> {
  const response = await http.patch<PublishJob>(`/publish/jobs/${jobId}`, payload);
  return response.data;
}

export async function publishJobToCreator(jobId: number): Promise<PublishJob> {
  const response = await http.post<PublishJob>(`/publish/jobs/${jobId}/publish`);
  return response.data;
}

export async function retryPublishJob(jobId: number): Promise<PublishJob> {
  const response = await http.post<PublishJob>(`/publish/jobs/${jobId}/retry`);
  return response.data;
}

export async function cancelPublishJob(jobId: number): Promise<PublishJob> {
  const response = await http.post<PublishJob>(`/publish/jobs/${jobId}/cancel`);
  return response.data;
}

export async function deletePublishJob(jobId: number): Promise<void> {
  await http.delete(`/publish/jobs/${jobId}`);
}

export async function fetchPublishAssets(jobId: number, signal?: AbortSignal): Promise<Paginated<PublishAsset>> {
  const response = await http.get<Paginated<PublishAsset>>(`/publish/jobs/${jobId}/assets`, { signal, timeout: 10000 });
  return response.data;
}

export async function addPublishAsset(jobId: number, payload: PublishAssetPayload): Promise<PublishAsset> {
  const response = await http.post<PublishAsset>(`/publish/jobs/${jobId}/assets`, payload);
  return response.data;
}

export async function deletePublishAsset(assetId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/publish/assets/${assetId}`);
  return response.data;
}

export async function uploadPublishAsset(assetId: number): Promise<PublishAsset> {
  const response = await http.post<PublishAsset>(`/publish/assets/${assetId}/upload`);
  return response.data;
}

export async function checkAccount(accountId: number): Promise<PlatformAccount> {
  const response = await http.post<PlatformAccount>(`/accounts/${accountId}/check`);
  return response.data;
}

export async function deleteAccount(accountId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/accounts/${accountId}`);
  return response.data;
}

function quietRequestOptions(signal?: AbortSignal) {
  return { signal, _silent: true };
}

function backgroundRequestOptions(signal?: AbortSignal) {
  return { ...quietRequestOptions(signal), timeout: 15000 };
}

export async function createXhsQrLogin(payload: {
  sub_type: "pc" | "creator";
  sync_creator?: boolean;
  account_id?: number;
}, signal?: AbortSignal): Promise<XhsQrLoginSession> {
  const response = await http.post<XhsQrLoginSession>(`/xhs/login-sessions/${payload.sub_type}/qrcode`, {
    sync_creator: payload.sync_creator, account_id: payload.account_id
  }, quietRequestOptions(signal));
  return response.data;
}

export async function importXhsAccountCookie(payload: {
  sub_type: "pc" | "creator";
  cookie_string: string;
  account_id?: number;
  sync_creator?: boolean;
}, signal?: AbortSignal): Promise<PlatformAccount> {
  const response = await http.post<PlatformAccount>("/accounts/import-cookie", {
    platform: "xhs", ...payload
  }, quietRequestOptions(signal));
  return response.data;
}

export async function pollXhsQrLogin(sessionId: number, signal?: AbortSignal): Promise<XhsQrLoginSession> {
  const response = await http.get<XhsQrLoginSession>(`/xhs/login-sessions/${sessionId}`, backgroundRequestOptions(signal));
  return response.data;
}

export async function prepareXhsPhoneLogin(accountId?: number, signal?: AbortSignal): Promise<{ preparation_token: string; expires_in: number }> {
  const response = await http.post<{ preparation_token: string; expires_in: number }>(
    "/xhs/login-sessions/pc/phone/prepare", { account_id: accountId }, quietRequestOptions(signal)
  );
  return response.data;
}

export async function sendXhsPhoneCode(payload: {
  sub_type: "pc" | "creator";
  phone: string;
  preparation_token?: string;
  sync_creator?: boolean;
  account_id?: number;
}, signal?: AbortSignal): Promise<{ session_id: number; status: string; message: string }> {
  const response = await http.post<{ session_id: number; status: string; message: string }>(
    `/xhs/login-sessions/${payload.sub_type}/phone/send-code`,
    { preparation_token: payload.preparation_token, phone: payload.phone, sync_creator: payload.sync_creator, account_id: payload.account_id },
    quietRequestOptions(signal)
  );
  return response.data;
}

export async function confirmXhsPhoneLogin(payload: {
  sub_type: "pc" | "creator";
  session_id: number;
  phone: string;
  code: string;
  sync_creator?: boolean;
}, signal?: AbortSignal): Promise<XhsQrLoginSession> {
  const response = await http.post<XhsQrLoginSession>(`/xhs/login-sessions/${payload.sub_type}/phone/confirm`, {
    session_id: payload.session_id,
    phone: payload.phone,
    code: payload.code,
    sync_creator: payload.sync_creator
  }, quietRequestOptions(signal));
  return response.data;
}

export async function fetchNotifications(params?: { unread?: boolean; page?: number; page_size?: number }): Promise<Paginated<AppNotification>> {
  const response = await http.get<Paginated<AppNotification>>("/notifications", { params });
  return response.data;
}

export async function markNotificationRead(id: number): Promise<AppNotification> {
  const response = await http.post<AppNotification>(`/notifications/${id}/read`);
  return response.data;
}

export async function markAllNotificationsRead(): Promise<{ marked: number }> {
  const response = await http.post<{ marked: number }>("/notifications/read-all");
  return response.data;
}

// Auto Tasks (Auto Operations)

export async function fetchAutoTasks(): Promise<Paginated<AutoTask>> {
  const response = await http.get<Paginated<AutoTask>>("/auto-tasks");
  return response.data;
}

export async function createAutoTask(payload: AutoTaskCreatePayload): Promise<AutoTask> {
  const response = await http.post<AutoTask>("/auto-tasks", payload);
  return response.data;
}

export async function updateAutoTask(taskId: number, payload: AutoTaskUpdatePayload): Promise<AutoTask> {
  const response = await http.patch<AutoTask>(`/auto-tasks/${taskId}`, payload);
  return response.data;
}

export async function deleteAutoTask(taskId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/auto-tasks/${taskId}`);
  return response.data;
}

export async function runAutoTask(taskId: number): Promise<AutoTaskRunResult> {
  const response = await http.post<AutoTaskRunResult>(`/auto-tasks/${taskId}/run`);
  return response.data;
}


export type ChannelMessage = {
  id: string;
  state: string;
  error: string;
  result: { output: { text: string; structured?: { reply?: string } | null } | null } | null;
};
export async function submitChannelMessage(data: { message_id: string; text: string; images: string[]; context: Record<string, unknown>; account_id?: number; agent_id?: string }): Promise<ChannelMessage> {
  return (await http.post<ChannelMessage>("/channel/messages", data)).data;
}
export async function fetchChannelMessage(id: string, account_id?: number): Promise<ChannelMessage> {
  return (await http.get<ChannelMessage>(`/channel/messages/${encodeURIComponent(id)}`, { params: { account_id } })).data;
}
export async function uploadChannelImage(file: File, account_id?: number): Promise<{ id: string }> {
  const data = new FormData(); data.append("file", file); if (account_id) data.append("account_id", String(account_id));
  return (await http.post<{ id: string }>("/channel/assets", data)).data;
}

export type MulticaRoute = {
  id: string; version: number; name: string; categories: string[]; kinds: string[];
  surfaces: string[]; event_types: string[]; grouping: string; agent_id: string;
  agent: { id: string; name: string; model: string; status: string } | null;
};
export type ModelRouting = {
  connected: boolean; error?: string; multica_url: string; routes: MulticaRoute[];
  bindings: Record<string, string | null>; choices: Record<string, string[]>; agent_choices?: Record<string, AgentChoice[]>;
};
export async function fetchModelRouting(account_id?: number): Promise<ModelRouting> {
  return (await http.get<ModelRouting>('/model-routing', { params: { account_id } })).data;
}
export async function saveModelRouting(purpose: string, agent_id: string, account_id: number) {
  return (await http.put(`/model-routing/${encodeURIComponent(purpose)}`, { agent_id, account_id })).data;
}
export type AgentChoice = { id: string; name: string; route_id: string };
export type AgentSnapshot = { id?: string; name?: string; route_id?: string; route_version?: number };
export async function fetchDmAgents(account_id: number, signal?: AbortSignal): Promise<{ connected: boolean; agents: AgentChoice[]; default_agent_id?: string }> {
  return (await http.get('/xhs/direct-messages/agents', { params: { account_id }, signal })).data;
}
export type DmControlMode = 'request_approval' | 'auto_approve' | 'full_access';
export type XhsNetworkStatus = {
  mode: 'direct' | 'home'; label: string;
  state: 'direct' | 'checking' | 'online' | 'offline' | 'restricted' | 'unconfigured';
  message: string; checked_at: string | null; last_online_at: string | null;
  exit_ip: string | null; authorization_expires_at: string | null; automatic_fallback: false;
};
export async function fetchXhsNetwork(signal?: AbortSignal): Promise<XhsNetworkStatus> {
  return (await http.get('/xhs/direct-messages/network', backgroundRequestOptions(signal))).data;
}
export async function checkXhsNetwork(): Promise<XhsNetworkStatus> {
  return (await http.post('/xhs/direct-messages/network/check')).data;
}
export type DmListener = {
  account_id: number; enabled: boolean; auto_reply: boolean; route_id: string; agent_id?: string; agent_snapshot?: AgentSnapshot;
  state: string; error: string; updated_at: string;
  control_mode: DmControlMode;
  conversation_holds?: { message_id: number; peer_id: string; state: 'uncertain' | 'held'; reason: string }[];
};
export type DmMediaRef = { asset_id: string; kind: 'image' | 'sticker' | 'emoji'; label: string; content_type: number; library?: boolean };
export async function fetchDmMedia(account_id: number, asset_id: string, signal?: AbortSignal): Promise<Blob> {
  return (await http.get(`/xhs/direct-messages/media/${asset_id}`, { ...backgroundRequestOptions(signal), params: { account_id }, responseType: 'blob' })).data;
}
export type DirectMessageRecord = {
  id: number; account_id: number; message_id: string; peer_id: string; text: string; agent_snapshot?: AgentSnapshot;
  media?: DmMediaRef[]; reply_message?: { type: string; media: DmMediaRef[] };
  reply_messages?: { type: string; text: string; media: DmMediaRef[]; status: string; delay_seconds: number }[];
  state: string; reply_text: string; channel_event_id: string; error: string;
  receipt: Record<string, unknown> & { network?: { mode: 'direct' | 'home'; label: string; checked_at: string; exit_ip: string | null } }; created_at: string;
  history?: { window_hours: number; total_messages: number; included_messages: number; complete: boolean; context_truncated?: boolean };
  analysis?: { decision?: string; summary?: string; intent?: string; style?: string; relationship_stage?: string };
};
export type DmHistoryResult = {
  items: { message_id: string; direction: 'inbound' | 'outbound'; text: string; sent_at: string; revoked: boolean; content_type: number; media?: DmMediaRef[] }[];
  history: { complete?: boolean; synced_at?: string; window_hours?: number };
  analysis: NonNullable<DirectMessageRecord['analysis']>;
};
export async function fetchDmHistory(account_id: number, peer_id: string): Promise<DmHistoryResult> {
  return (await http.get('/xhs/direct-messages/history', { params: { account_id, peer_id } })).data;
}

export type XhsChatConversation = { peer_id: string; nickname: string; avatar: string; last_text: string; last_at: string | null; syncing: boolean };
export type XhsChatConnection = { state: string; error: string; manual_enabled: boolean; auto_reply: boolean };
export type XhsChatMessage = DmHistoryResult['items'][number] & { key: string; state: string; error?: string; manual_id?: number; client_id?: string };
export type XhsChatSend = { client_id: string; expected_external_user_id: string; peer_id: string; text: string; asset_id?: string };
export async function fetchXhsChats(account_id: number, signal?: AbortSignal): Promise<{ items: XhsChatConversation[]; connection: XhsChatConnection }> {
  return (await http.get('/xhs/chat/conversations', { ...backgroundRequestOptions(signal), params: { account_id } })).data;
}
export async function fetchXhsChatMessages(account_id: number, peer_id: string, signal?: AbortSignal): Promise<{ items: XhsChatMessage[]; syncing: boolean; manual_control: boolean }> {
  return (await http.get('/xhs/chat/messages', { ...backgroundRequestOptions(signal), params: { account_id, peer_id } })).data;
}
export async function connectXhsChat(account_id: number, enabled: boolean) {
  return (await http.post(`/xhs/chat/accounts/${account_id}/connect`, { enabled })).data;
}
export async function refreshXhsChat(account_id: number, peer_id?: string) {
  return (await http.post(`/xhs/chat/accounts/${account_id}/refresh`, { peer_id })).data;
}
export async function sendXhsChat(account_id: number, payload: XhsChatSend) {
  return (await http.post(`/xhs/chat/accounts/${account_id}/send`, payload, backgroundRequestOptions())).data;
}
export async function checkXhsChatSend(account_id: number, client_id: string, signal?: AbortSignal) {
  return (await http.get(`/xhs/chat/requests/${encodeURIComponent(client_id)}`, { ...backgroundRequestOptions(signal), params: { account_id } })).data;
}
export async function uploadXhsChatImage(account_id: number, file: File): Promise<DmMediaRef> {
  const data = new FormData(); data.append('file', file);
  return (await http.post(`/xhs/chat/accounts/${account_id}/images`, data)).data;
}
export async function controlXhsChat(account_id: number, peer_id: string, enabled: boolean) {
  return (await http.post(`/xhs/chat/accounts/${account_id}/control`, { peer_id, enabled })).data;
}
export async function resolveXhsChatSend(id: number, outcome: 'confirmed_sent' | 'skip') {
  return (await http.post(`/xhs/chat/requests/${id}/resolve`, { outcome })).data;
}
export async function reanalyzeDirectMessage(id: number) {
  return (await http.post(`/xhs/direct-messages/${id}/reanalyze`)).data;
}
export async function fetchDmListeners(signal?: AbortSignal): Promise<{ items: DmListener[] }> {
  return (await http.get('/xhs/direct-messages/listeners', backgroundRequestOptions(signal))).data;
}
export async function configureDmListener(account_id: number, data: { enabled: boolean; auto_reply: boolean; agent_id?: string; route_id?: string; control_mode?: DmControlMode }): Promise<DmListener> {
  return (await http.put(`/xhs/direct-messages/listeners/${account_id}`, data)).data;
}
export async function fetchDirectMessages(account_id: number, signal?: AbortSignal): Promise<{ items: DirectMessageRecord[] }> {
  return (await http.get('/xhs/direct-messages', { ...backgroundRequestOptions(signal), params: { account_id } })).data;
}
export async function resolveDirectMessage(id: number, outcome: 'confirmed_sent' | 'skip') {
  return (await http.post(`/xhs/direct-messages/${id}/resolve`, { outcome })).data;
}
export async function approveDirectMessage(id: number) {
  return (await http.post(`/xhs/direct-messages/${id}/approve`)).data;
}


export type CommentMonitoring = {
  account_id: number; nickname: string; configured: boolean; enabled?: boolean;
  account_status?: string; account_problem?: string;
  automation_enabled?: boolean; auto_reply?: boolean; control_mode?: DmControlMode; route_id?: string; agent_id?: string; agent_snapshot?: AgentSnapshot;
  state?: string; poll_seconds?: number; heartbeat?: string; checked_at?: string;
  error?: string; counts?: Record<string, number>; pending?: number; schedule_error?: boolean;
  verified_visible?: number; sending_paused?: boolean; sending_pause_reason?: string; visibility_warning?: string; verification_error?: string;
  schedule: Array<{ day: string; slots: string[] }>;
  items: Array<{ id: string; stage: string; text: string; target_text: string; note_title: string; agent_snapshot?: AgentSnapshot;
    note_id: string; comment_id: string; peer_id: string; kind: string; reply_text: string;
    reply_comment_id: string; reply_version: string; reason: string; detail: string; created_at: string; updated_at: string;
    verification?: { state?: string; checked_at?: string; observer_role?: string; sender_visible?: boolean; observer_visible?: boolean } }>;
};

export async function fetchCommentMonitoring(accountId: number, signal?: AbortSignal): Promise<CommentMonitoring> {
  const response = await http.get<CommentMonitoring>("/xhs/comments/monitoring", {
    params: { account_id: accountId }, signal, timeout: 10000,
  });
  return response.data;
}

export async function fetchCommentRoutes(accountId: number, signal?: AbortSignal): Promise<{ connected: boolean; agents: AgentChoice[]; default_agent_id?: string; routes: Array<{ id: string; name: string; agent_name: string }> }> {
  return (await http.get('/xhs/comments/routes', { params: { account_id: accountId }, signal, timeout: 10000 })).data;
}
export async function configureCommentListener(accountId: number, data: { enabled: boolean; auto_reply: boolean; control_mode: DmControlMode; agent_id?: string; route_id?: string }): Promise<CommentMonitoring> {
  return (await http.put(`/xhs/comments/listeners/${accountId}`, data, { timeout: 25000 })).data;
}
export async function approveCommentReply(accountId: number, entryId: string, replyVersion: string) {
  return (await http.post(`/xhs/comments/listeners/${accountId}/entries/${encodeURIComponent(entryId)}/approve`, { reply_version: replyVersion })).data;
}
export async function skipCommentReply(accountId: number, entryId: string) {
  return (await http.post(`/xhs/comments/listeners/${accountId}/entries/${encodeURIComponent(entryId)}/skip`)).data;
}

export type ProactiveSource = 'recommend' | 'visitors' | 'outreach';
export type ProactiveSettings = {
  source: ProactiveSource; instruction: string; enabled: boolean;
  interval_minutes: number; browse_limit: number; comment_limit: number;
  keyword: string; comments_per_note: number; dm_limit: number; share_note: boolean; filter_ads: boolean;
  daily_start: string; daily_end: string;
  agent_id: string; agent_snapshot?: AgentSnapshot;
  revision?: string; next_at?: string;
};
export type ProactiveSnapshot = {
  strategies: Record<ProactiveSource, ProactiveSettings>;
  runs: Array<{ id: string; source: ProactiveSource; state: string; detail: string; created_at: string;
    diagnostics?: { scanned?: number; prepared?: number; detail_reads?: number; read_budget_reached?: boolean;
      counts?: Record<string, number>; records?: Array<{ note_id: string; title: string; author_name: string; outcome: string; reason: string }> } }>;
  items: Array<{ id: string; source: ProactiveSource; account_nickname?: string; state: string; detail: string;
    external_id: string; created_at: string; sent_at?: string; attempted_at?: string;
    interaction?: 'comment' | 'dm'; sequence?: number; keyword?: string; share_note?: boolean; share_note_url?: string;
    agent_snapshot?: AgentSnapshot;
    messages?: Array<{ kind: string; state: string; external_id: string; detail: string }>;
    title: string; author_name: string; note_url: string; text: string; reason: string; references: string[]; reference_status?: string }>;
};
export async function fetchProactive(accountId: number, signal?: AbortSignal): Promise<ProactiveSnapshot> {
  return (await http.get(`/xhs/proactive/${accountId}`, { signal, timeout: 10000 })).data;
}
export async function fetchProactiveAgents(accountId: number, signal?: AbortSignal): Promise<{ connected: boolean; agents: AgentChoice[] }> {
  return (await http.get(`/xhs/proactive/${accountId}/agents`, { signal, timeout: 10000 })).data;
}
export async function saveProactive(accountId: number, data: ProactiveSettings): Promise<ProactiveSettings> {
  return (await http.put(`/xhs/proactive/${accountId}/settings/${data.source}`, data)).data;
}
export async function runProactive(accountId: number, source: ProactiveSource) {
  return (await http.post(`/xhs/proactive/${accountId}/run/${source}`)).data;
}
