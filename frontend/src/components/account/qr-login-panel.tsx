import { Alert, Button, Checkbox, Space, Typography } from "antd";
import axios from "axios";
import { useEffect, useRef, useState } from "react";
import { createXhsQrLogin, pollXhsQrLogin } from "../../lib/api";
import type { PlatformAccount, XhsQrLoginSession } from "../../types";

export function QrLoginPanel({ accountType, accountId, onConfirmed }: {
  accountType: "pc" | "creator";
  accountId?: number;
  onConfirmed: (account: PlatformAccount) => void;
}) {
  const [session, setSession] = useState<XhsQrLoginSession | null>(null);
  const [syncCreator, setSyncCreator] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const request = useRef<AbortController | null>(null);
  const confirmed = useRef(onConfirmed);
  confirmed.current = onConfirmed;
  useEffect(() => () => { generation.current++; request.current?.abort(); }, []);

  async function generate() {
    const current = ++generation.current;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true); setError(null); setSession(null);
    try {
      const result = await createXhsQrLogin({ sub_type: accountType, account_id: accountId,
        sync_creator: accountType === "pc" && syncCreator }, controller.signal);
      if (current === generation.current) setSession(result);
    } catch (caught) {
      if (!controller.signal.aborted) setError(axios.isAxiosError(caught) && typeof caught.response?.data?.detail === "string"
        ? caught.response.data.detail : "二维码获取失败，请重新获取。");
    } finally {
      if (current === generation.current) setBusy(false);
    }
  }

  const sessionId = session?.session_id;
  const status = session?.status;
  useEffect(() => {
    if (!sessionId || status === "confirmed" || status === "expired") return;
    const controller = new AbortController();
    const current = generation.current;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await pollXhsQrLogin(sessionId, controller.signal);
        if (controller.signal.aborted || current !== generation.current) return;
        setError(null);
        setSession(previous => ({ ...previous, ...result }));
        if (result.status === "confirmed") {
          if (result.account) confirmed.current(result.account);
          else setError("登录已确认，请刷新账号矩阵核对账号。");
          if (accountType === "pc" && syncCreator && !result.creator_account)
            setError("PC 已绑定，Creator 尚未同步。请切换 Creator 后另行扫码，发布需要 Creator 登录。");
          return;
        }
        if (result.status === "expired") return;
      } catch (caught) {
        if (controller.signal.aborted || current !== generation.current) return;
        setError(axios.isAxiosError(caught) && typeof caught.response?.data?.detail === "string"
          ? caught.response.data.detail : "暂时无法检查扫码状态，正在重试。");
        if (axios.isAxiosError(caught) && ([400, 401, 403, 404, 409].includes(caught.response?.status || 0)
          || String(caught.response?.data?.detail || "").includes("HTTP 471"))) return;
      }
      timer = setTimeout(poll, 3000);
    };
    timer = setTimeout(poll, 3000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [sessionId, status, accountType, syncCreator]);

  return <Space direction="vertical" size="middle" style={{ width: "100%" }}>
    <Alert type="info" showIcon message="使用小红书 App 扫码"
      description="先在手机小红书 App 中用 QQ 登录目标账号，再用该账号的扫一扫扫描下方二维码，并在手机确认。QQ 邮箱不能用于接收这里的短信验证码。" />
    {accountType === "pc" && <Checkbox checked={syncCreator} disabled={busy || Boolean(session && !["confirmed", "expired"].includes(session.status))}
      onChange={event => setSyncCreator(event.target.checked)}>同时同步 Creator，用于发布作品</Checkbox>}
    {session?.qr_image_data_url && <img src={session.qr_image_data_url} alt="小红书登录二维码" style={{ width: 240, maxWidth: "100%", alignSelf: "center" }} />}
    {session && <Typography.Text>{session.status === "confirmed" ? "登录已确认" : session.status === "scanned" ? "已扫码，请在手机确认登录" : session.status === "expired" ? "二维码已过期，请重新获取" : "等待扫码"}</Typography.Text>}
    {error && <Alert type="error" showIcon message={error} />}
    <Button block loading={busy} onClick={generate}>{session ? "重新获取二维码" : "获取登录二维码"}</Button>
  </Space>;
}
