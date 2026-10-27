import { Alert, Button, Checkbox, Form, Input, Space, Typography } from "antd";
import { MessageOutlined, CheckCircleOutlined } from "@ant-design/icons";
import axios from "axios";
import { useEffect, useRef, useState } from "react";

import { confirmXhsPhoneLogin, sendXhsPhoneCode, prepareXhsPhoneLogin } from "../../lib/api";
import type { PlatformAccount } from "../../types";

const { Text } = Typography;

type PhoneLoginPanelProps = {
  accountType: "pc" | "creator";
  accountId?: number;
  onConfirmed: (account: PlatformAccount) => void;
};

const PHONE_CODE_COOLDOWN_SECONDS = 120;

export function PhoneLoginPanel({ accountType, accountId, onConfirmed }: PhoneLoginPanelProps) {
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [statusText, setStatusText] = useState("输入手机号后发送验证码");
  const [isSending, setIsSending] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);
  const [cooldownSeconds, setCooldownSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [preparation, setPreparation] = useState<{ token: string; expiresAt: number } | null>(null);
  const [isPreparing, setIsPreparing] = useState(false);
  const [prepareError, setPrepareError] = useState<string | null>(null);
  const [prepareVersion, setPrepareVersion] = useState(0);
  const [syncCreator, setSyncCreator] = useState(false);
  const isCoolingDown = cooldownSeconds > 0;
  const requestRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (accountType !== "pc" || isCoolingDown || isSending) return;
    const controller = new AbortController();
    let timer: number | undefined;
    setIsPreparing(true);
    setPreparation(null);
    setPrepareError(null);
    void prepareXhsPhoneLogin(accountId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setPreparation({ token: result.preparation_token, expiresAt: Date.now() + result.expires_in * 1000 });
      setIsPreparing(false);
      timer = window.setTimeout(() => setPrepareVersion((v) => v + 1), result.expires_in * 1000);
    }).catch((caught) => {
      if (controller.signal.aborted) return;
      setPrepareError(errorMessage(caught, "登录准备失败，请重新准备。"));
      setIsPreparing(false);
    });
    return () => { controller.abort(); if (timer !== undefined) window.clearTimeout(timer); };
  }, [accountType, accountId, prepareVersion, isCoolingDown, isSending]);

  useEffect(() => () => requestRef.current?.abort(), []);

  function errorMessage(caught: unknown, fallback: string) {
    if (axios.isAxiosError(caught) && typeof caught.response?.data?.detail === "string") {
      return caught.response.data.detail;
    }
    return fallback;
  }

  useEffect(() => {
    if (cooldownSeconds <= 0) {
      return;
    }

    const timer = window.setTimeout(() => {
      setCooldownSeconds((current) => Math.max(0, current - 1));
    }, 1000);

    return () => window.clearTimeout(timer);
  }, [cooldownSeconds]);

  async function handleSendCode() {
    setError(null);
    if (isSending || isConfirming || isCoolingDown || isPreparing) {
      return;
    }
    if (phone.trim().length < 6) {
      setError("请输入有效手机号。");
      return;
    }

    if (accountType === "pc" && (!preparation || preparation.expiresAt <= Date.now())) {
      setPrepareVersion((v) => v + 1);
      return;
    }
    setIsSending(true);
    setStatusText("正在向小红书请求验证码，请稍候");
    setSessionId(null);
    const controller = new AbortController();
    requestRef.current = controller;
    try {
      const result = await sendXhsPhoneCode({
        sub_type: accountType,
        phone: phone.trim(),
        preparation_token: accountType === "pc" ? preparation?.token : undefined,
        account_id: accountId,
        sync_creator: accountType === "pc" ? syncCreator : undefined
      }, controller.signal);
      if (controller.signal.aborted) return;
      if (accountType === "pc") setPreparation(null);
      setSessionId(result.session_id);
      setStatusText("小红书已接受验证码请求，请查看手机短信");
      setCooldownSeconds(PHONE_CODE_COOLDOWN_SECONDS);
    } catch (caught) {
      if (!controller.signal.aborted) {
        setError(errorMessage(caught, "验证码发送失败，请检查网络后重试。"));
        if (accountType === "pc") { setPreparation(null); setPrepareVersion((v) => v + 1); }
      }
    } finally {
      if (!controller.signal.aborted) setIsSending(false);
    }
  }

  async function handleConfirm() {
    if (isSending || isConfirming) return;
    setError(null);
    if (!sessionId) {
      setError("请先发送验证码。");
      return;
    }
    if (code.trim().length < 4) {
      setError("请输入短信验证码。");
      return;
    }

    setIsConfirming(true);
    const controller = new AbortController();
    requestRef.current = controller;
    try {
      const result = await confirmXhsPhoneLogin({
        sub_type: accountType,
        session_id: sessionId,
        phone: phone.trim(),
        code: code.trim(),
        sync_creator: accountType === "pc" ? syncCreator : undefined
      }, controller.signal);
      if (controller.signal.aborted) return;
      if (result.account) {
        onConfirmed(result.account);
      }
      setStatusText(accountId ? "账号已重新登录" : "账号绑定成功");
    } catch (caught) {
      if (!controller.signal.aborted) setError(errorMessage(caught, "验证码校验失败或已过期。"));
    } finally {
      if (!controller.signal.aborted) setIsConfirming(false);
    }
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Text style={{ color: "var(--ui-muted)" }}>{isPreparing ? "正在提前准备登录环境，可以先填写手机号" : statusText}</Text>

      <Form layout="vertical">
        <Form.Item label={<span style={{ color: "var(--ui-muted)" }}>手机号</span>}>
          <Input
            value={phone}
            disabled={isSending || isConfirming}
            onChange={(e) => {
              setPhone(e.target.value);
              setSessionId(null);
              setCode("");
              setStatusText("输入手机号后发送验证码");
            }}
            placeholder="请输入手机号"
            style={{ background: "var(--ui-surface,#1f1f1f)", borderColor: "var(--ui-line,#303030)", color: "var(--ui-muted)" }}
          />
        </Form.Item>
      </Form>

      {accountType === "pc" ? (
        <Checkbox
          checked={syncCreator}
          onChange={(event) => setSyncCreator(event.target.checked)}
          style={{ color: "var(--ui-muted)" }}
        >
          登录 PC 后同步 Creator 账号
        </Checkbox>
      ) : null}

      <Button
        block
        icon={<MessageOutlined />}
        onClick={handleSendCode}
        disabled={isSending || isConfirming || isCoolingDown || isPreparing || (accountType === "pc" && !preparation)}
        loading={isSending || isPreparing}
      >
        {isPreparing ? "准备中..." : isSending ? "发送中..." : isCoolingDown ? `${cooldownSeconds} 秒后重发` : "发送验证码"}
      </Button>

      <Form layout="vertical">
        <Form.Item label={<span style={{ color: "var(--ui-muted)" }}>验证码</span>}>
          <Input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="请输入验证码"
            style={{ background: "var(--ui-surface,#1f1f1f)", borderColor: "var(--ui-line,#303030)", color: "var(--ui-muted)" }}
          />
        </Form.Item>
      </Form>

      {prepareError ? <Alert type="error" message={prepareError} action={<Button size="small" onClick={() => setPrepareVersion((v) => v + 1)}>重新准备</Button>} showIcon /> : null}
      {error ? <Alert type="error" message={error} showIcon /> : null}

      <Button
        type="primary"
        block
        icon={<CheckCircleOutlined />}
        onClick={handleConfirm}
        disabled={!sessionId || isSending}
        loading={isConfirming}
      >
        {isConfirming ? "验证中..." : accountId ? "确认重新登录" : "确认绑定"}
      </Button>
    </Space>
  );
}
