import { Alert, Button, Checkbox, Input, Space, Typography } from "antd";
import axios from "axios";
import { useEffect, useRef, useState } from "react";
import { importXhsAccountCookie } from "../../lib/api";
import type { PlatformAccount } from "../../types";

export function CookieLoginPanel({ accountType, accountId, onConfirmed }: {
  accountType: "pc" | "creator";
  accountId?: number;
  onConfirmed: (account: PlatformAccount) => void;
}) {
  const [cookie, setCookie] = useState("");
  const [syncCreator, setSyncCreator] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function submit() {
    if (busy || !cookie.trim()) return;
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setError(null);
    try {
      const account = await importXhsAccountCookie({ sub_type: accountType, cookie_string: cookie.trim(),
        account_id: accountId, sync_creator: accountType === "pc" && syncCreator }, controller.signal);
      if (!controller.signal.aborted) { setCookie(""); onConfirmed(account); }
    } catch (caught) {
      if (!controller.signal.aborted) setError(axios.isAxiosError(caught) && typeof caught.response?.data?.detail === "string"
        ? caught.response.data.detail : "导入未完成，请确认已在官方网页登录目标账号。");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  const officialUrl = accountType === "pc" ? "https://www.xiaohongshu.com/explore" : "https://creator.xiaohongshu.com/";
  return <Space direction="vertical" size="middle" style={{ width: "100%" }}>
    <Alert type="info" showIcon message="先在官方网页完成登录验证"
      description="扫码受限时，可导入已在官方网页验证通过的同一账号会话。系统会核对实际登录身份后加入矩阵。" />
    <Typography.Link href={officialUrl} target="_blank" rel="noopener noreferrer">打开小红书{accountType === "pc" ? "官方网页" : "创作服务平台"}</Typography.Link>
    <Typography.Text>登录后按 F12，打开 Network（网络），刷新页面，选择发往小红书的请求，在 Request Headers（请求标头）中复制 Cookie 的值，仅粘贴到下方。</Typography.Text>
    <Input.Password value={cookie} onChange={event => setCookie(event.target.value)} disabled={busy}
      autoComplete="off" placeholder="粘贴已登录账号的 Cookie" aria-label="已登录账号的 Cookie" />
    <Typography.Text type="secondary">不需要 QQ 密码。Cookie 仅通过当前工作台提交验证，不要发到聊天或其他网站。</Typography.Text>
    {accountType === "pc" && <Checkbox checked={syncCreator} disabled={busy}
      onChange={event => setSyncCreator(event.target.checked)}>同时尝试同步 Creator 账号</Checkbox>}
    {error && <Alert type="error" showIcon message={error} />}
    <Button type="primary" block loading={busy} disabled={!cookie.trim() || busy} onClick={submit}>验证并加入账号矩阵</Button>
  </Space>;
}
