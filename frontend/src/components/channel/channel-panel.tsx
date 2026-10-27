import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Input, Select, Space, Typography } from "antd";
import { fetchAccounts, fetchModelRouting, fetchChannelMessage, submitChannelMessage, uploadChannelImage } from "../../lib/api";

export function ChannelPanel({ context, onAdopt }: { context: Record<string, unknown>; onAdopt: (text: string) => void }) {
  const [accounts, setAccounts] = useState<Array<{ id: number; nickname: string }>>([]);
  const [accountId, setAccountId] = useState<number>();
  const [agentId, setAgentId] = useState<string>();
  const [directoryVersion, setDirectoryVersion] = useState(0);
  const directoryAccount = useRef(accountId);
  const [agents, setAgents] = useState<Array<{ id: string; name: string }>>([]);
  useEffect(() => { void fetchAccounts('xhs').then(all => { const pc = all.filter(a => a.sub_type === 'pc'); setAccounts(pc); setAccountId(pc[0]?.id); }); }, []);
  useEffect(() => {
    setAgents([]); if (directoryAccount.current !== accountId) setAgentId(undefined); directoryAccount.current = accountId;
    if (!accountId) return;
    let active = true;
    void fetchModelRouting(accountId).then(data => { if (active) { setAgents(data.agent_choices?.content || []); setAgentId(current => data.agent_choices?.content?.some(a => a.id === current) ? current : data.routes.find(r => r.id === data.bindings.content)?.agent?.id); } }).catch(() => { if (active) setStatus('Agent 列表读取失败，请重新选择账号'); });
    return () => { active = false; };
  }, [accountId, directoryVersion]);
  const [instruction, setInstruction] = useState("分析当前草稿，给出可以直接采用的中文优化版本。");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [output, setOutput] = useState("");
  const alive = useRef(true);
  const version = useRef(0);
  const contextKey = JSON.stringify(context);
  useEffect(() => { version.current++; setOutput(""); setStatus(""); setBusy(false); }, [contextKey, accountId, agentId]);
  const request = useRef<{ id: string; payload: string } | null>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function send() {
    if (!accountId || !agentId) return;
    const turn = version.current;
    setBusy(true); setStatus("正在提交 Channel…"); setOutput("");
    try {
      if (files.length > 6) throw new Error("最多上传 6 张图片");
      const images: string[] = [];
      for (const file of files) images.push((await uploadChannelImage(file, accountId)).id);
      if (!alive.current || turn !== version.current) return;
      const payload = JSON.stringify({ text: instruction, context, images, account_id: accountId, agent_id: agentId });
      if (!request.current || request.current.payload !== payload) request.current = { id: crypto.randomUUID(), payload };
      let message = await submitChannelMessage({ message_id: request.current.id, text: instruction, context, images, account_id: accountId, agent_id: agentId });
      for (let i = 0; i < 180 && alive.current && turn === version.current; i++) {
        if (message.result) {
          if (message.result.output) { setOutput(message.result.output.structured?.reply || message.result.output.text); setStatus("模型已返回，可采用到草稿编辑器。"); }
          else setStatus(message.error || "模型没有返回正文");
          return;
        }
        if (["blocked_no_route", "blocked_reply", "failed_preflight", "uncertain", "attention"].includes(message.state)) throw new Error(message.error);
        setStatus("请求已入队，正在等待 Multica 智能体…");
        await new Promise(resolve => setTimeout(resolve, 2000));
        message = await fetchChannelMessage(message.id, accountId);
      }
      if (alive.current && turn === version.current) setStatus("任务仍在后台处理，可在 Channel 查看。");
    } catch (error) {
      if (alive.current && turn === version.current) setStatus(error instanceof Error ? error.message : "Channel 请求未完成");
    } finally { if (alive.current && turn === version.current) setBusy(false); }
  }
  return <Card title="Channel 智能体" size="small" style={{ marginBottom: 16 }}>
    <Space orientation="vertical" style={{ width: "100%" }}>
      <Typography.Text type="secondary">携带当前草稿上下文，经消息中枢分流到 Multica。</Typography.Text>
      <Select aria-label="执行账号" value={accountId} disabled={busy} onChange={setAccountId} placeholder="选择账号" style={{ width: '100%' }} options={accounts.map(a => ({ value: a.id, label: a.nickname }))} />
      <Select aria-label="执行 Agent" value={agentId} disabled={busy} onChange={setAgentId} placeholder="选择当前账号 Agent" style={{ width: '100%' }} options={agents.map(a => ({ value: a.id, label: a.name }))} />
      <Button size="small" disabled={busy || !accountId} onClick={() => setDirectoryVersion(v => v + 1)}>刷新 Agent 目录</Button>
      <Input.TextArea value={instruction} onChange={e => setInstruction(e.target.value)} rows={3} maxLength={4000} aria-label="Channel 处理要求" />
      <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple onChange={e => setFiles(Array.from(e.target.files || []))} aria-label="Channel 业务图片" />
      <Button onClick={() => void send()} loading={busy} disabled={!agentId || !accountId || !agents.some(a => a.id === agentId)}>交给 Agent 处理</Button>
      {status && <Alert title={status} type="info" />}
      {output && <><Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>{output}</Typography.Paragraph><Button onClick={() => onAdopt(output)}>采用到编辑器</Button></>}
      <Typography.Link href="https://d.58nft.net:9456/" target="_blank" rel="noopener noreferrer">打开 Channel 消息中枢 ↗</Typography.Link>
    </Space>
  </Card>;
}
