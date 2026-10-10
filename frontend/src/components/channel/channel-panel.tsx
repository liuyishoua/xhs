import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Input, Space, Typography } from "antd";
import { fetchChannelMessage, submitChannelMessage, uploadChannelImage } from "../../lib/api";

export function ChannelPanel({ context, onAdopt }: { context: Record<string, unknown>; onAdopt: (text: string) => void }) {
  const [instruction, setInstruction] = useState("分析当前草稿，给出可以直接采用的中文优化版本。");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [output, setOutput] = useState("");
  const alive = useRef(true);
  const version = useRef(0);
  const contextKey = JSON.stringify(context);
  useEffect(() => { version.current++; setOutput(""); setStatus(""); setBusy(false); }, [contextKey]);
  const request = useRef<{ id: string; payload: string } | null>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function send() {
    const turn = version.current;
    setBusy(true); setStatus("正在提交 Channel…"); setOutput("");
    try {
      if (files.length > 6) throw new Error("最多上传 6 张图片");
      const images: string[] = [];
      for (const file of files) images.push((await uploadChannelImage(file)).id);
      if (!alive.current || turn !== version.current) return;
      const payload = JSON.stringify({ text: instruction, context, images });
      if (!request.current || request.current.payload !== payload) request.current = { id: crypto.randomUUID(), payload };
      let message = await submitChannelMessage({ message_id: request.current.id, text: instruction, context, images });
      for (let i = 0; i < 180 && alive.current && turn === version.current; i++) {
        if (message.result) {
          if (message.result.output) { setOutput(message.result.output.structured?.reply || message.result.output.text); setStatus("模型已返回，可采用到草稿编辑器。"); }
          else setStatus(message.error || "模型没有返回正文");
          return;
        }
        if (["blocked_no_route", "blocked_reply", "failed_preflight", "uncertain", "attention"].includes(message.state)) throw new Error(message.error);
        setStatus("请求已入队，正在等待 Multica 智能体…");
        await new Promise(resolve => setTimeout(resolve, 2000));
        message = await fetchChannelMessage(message.id);
      }
      if (alive.current && turn === version.current) setStatus("任务仍在后台处理，可在 Channel 查看。");
    } catch (error) {
      if (alive.current && turn === version.current) setStatus(error instanceof Error ? error.message : "Channel 请求未完成");
    } finally { if (alive.current && turn === version.current) setBusy(false); }
  }
  return <Card title="Channel 智能体" size="small" style={{ marginBottom: 16 }}>
    <Space orientation="vertical" style={{ width: "100%" }}>
      <Typography.Text type="secondary">携带当前草稿上下文，经消息中枢分流到 Multica。</Typography.Text>
      <Input.TextArea value={instruction} onChange={e => setInstruction(e.target.value)} rows={3} maxLength={4000} aria-label="Channel 处理要求" />
      <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple onChange={e => setFiles(Array.from(e.target.files || []))} aria-label="Channel 业务图片" />
      <Button onClick={() => void send()} loading={busy}>交给 Agent 处理</Button>
      {status && <Alert title={status} type="info" />}
      {output && <><Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>{output}</Typography.Paragraph><Button onClick={() => onAdopt(output)}>采用到编辑器</Button></>}
      <Typography.Link href="https://d.58nft.net:9456/" target="_blank" rel="noopener noreferrer">打开 Channel 消息中枢 ↗</Typography.Link>
    </Space>
  </Card>;
}
