import { Alert, Drawer, Segmented, message } from "antd";
import { useState } from "react";

import type { PlatformAccount } from "../../types";
import { PhoneLoginPanel } from "./phone-login-panel";
import { QrLoginPanel } from "./qr-login-panel";
import { CookieLoginPanel } from "./cookie-login-panel";

type AddAccountDrawerProps = {
  open: boolean;
  account?: PlatformAccount | null;
  onClose: () => void;
  onBound: () => void;
};

type AccountType = "pc" | "creator";

const accountTypeOptions = [
  { label: "PC", value: "pc" as const },
  { label: "Creator", value: "creator" as const },
];

export function AddAccountDrawer({ open, account, onClose, onBound }: AddAccountDrawerProps) {
  const [selectedAccountType, setAccountType] = useState<AccountType>("pc");
  const [loginMethod, setLoginMethod] = useState("qr");
  const accountType: AccountType = account?.sub_type === "creator" ? "creator" : account ? "pc" : selectedAccountType;

  function handleConfirmed(confirmedAccount: PlatformAccount) {
    if (account) {
      message.success(`${account.nickname || "账号"} 已重新登录`);
      onBound();
      onClose();
      return;
    }
    const actionText = confirmedAccount.action === "updated" ? "已更新到账号矩阵" : "已加入账号矩阵";
    message.success(`${confirmedAccount.nickname || "账号"} ${actionText}`);
    onBound();
  }

  return (
    <Drawer
      title={
        <div>
          <div style={{ fontSize: 12, color: "var(--ui-muted)", marginBottom: 4, textTransform: "uppercase", letterSpacing: 1 }}>
            XHS Account
          </div>
          <div style={{ fontSize: 18, fontWeight: 600, color: "var(--ui-muted)" }}>{account ? "重新登录小红书账号" : "添加小红书账号"}</div>
        </div>
      }
      placement="right"
      width={420}
      open={open}
      onClose={onClose}
      destroyOnClose
      styles={{
        header: { background: "var(--ui-surface,#1f1f1f)", borderBottom: "1px solid var(--ui-line,#303030)" },
        body: { background: "var(--ui-surface,#141414)", padding: 24 },
      }}
    >
      {account ? (
        <Alert
          type="info"
          showIcon
          message={`重新登录：${account.nickname || account.external_user_id}`}
          description="请使用原小红书账号完成登录。成功后保留账号、历史记录和监听设置，已启用的私信监听会自动重连。"
          style={{ marginBottom: 20 }}
        />
      ) : null}
      <div style={{ marginBottom: 20 }}>
        <Segmented
          block
          disabled={Boolean(account)}
          value={accountType}
          options={accountTypeOptions}
          onChange={(val) => setAccountType(val as AccountType)}
        />
      </div>

      <Segmented block style={{ marginBottom: 20 }} value={loginMethod}
        options={[{ label: "扫码登录", value: "qr" }, { label: "短信登录", value: "phone" }, { label: "Cookie 导入", value: "cookie" }]}
        onChange={value => setLoginMethod(String(value))} />
      {loginMethod === "qr" ? <QrLoginPanel key={accountType} accountType={accountType}
        accountId={account?.id} onConfirmed={handleConfirmed} /> : loginMethod === "cookie" ? <CookieLoginPanel
        key={accountType} accountType={accountType} accountId={account?.id} onConfirmed={handleConfirmed} /> : <PhoneLoginPanel
        key={accountType}
        accountType={accountType}
        accountId={account?.id}
        onConfirmed={handleConfirmed}
      />}
    </Drawer>
  );
}
