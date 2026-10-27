import { usePageVisible } from '../../../hooks/use-page-visible';
import { ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Col, Empty, Popconfirm, Row, Select, Space, Switch, Table, Tag, Typography } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader } from '../../../components/layout/app-shell';
import { readAccountSelection, saveAccountSelection, approveCommentReply, configureCommentListener, fetchAccounts, fetchCommentMonitoring, fetchCommentRoutes, skipCommentReply } from '../../../lib/api';
import type { CommentMonitoring, DmControlMode } from '../../../lib/api';
import type { PlatformAccount } from '../../../types';
import { formatShanghaiTime } from '../../../lib/time';
const states: Record<string, [string, string]> = {
  received: ['待分析', 'default'], thinking: ['正在生成回复', 'blue'], ready: ['待发送', 'cyan'],
  sending: ['发送中', 'blue'], replied: ['已核验原帖可见', 'green'],
  approval_pending: ['待批准发送', 'gold'], held: ['特殊事项待处理', 'orange'],
  submitted: ['已提交 · 待核验', 'blue'], verification_pending: ['可见性待核验', 'orange'], not_visible: ['原帖可见性异常', 'red'], no_reply: ['不回复', 'default'],
  stopped: ['已停止联系', 'default'], rejected: ['处理失败', 'red'], uncertain: ['回执待核对', 'orange'],
};
const healthStates: Record<string, [string, string]> = {
  listening: ['正在监听', 'green'], paused: ['已暂停', 'default'], error: ['通知读取失败', 'red'],
  stale: ['监听未及时更新', 'orange'], starting: ['正在启动', 'blue'],
  stopped: ['已停止监听', 'default'],
  login_expired: ['登录已过期', 'red'], account_unavailable: ['账号待恢复', 'red'],
};
const controlModes: Record<DmControlMode, { label: string; description: string }> = {
  request_approval: { label: '请求批准', description: '模型生成回复，每条正文经你批准后发送。' },
  auto_approve: { label: '帮我批准', description: '模型自动处理普通评论，特殊事项交给你。' },
  full_access: { label: '完全访问', description: '模型自主决定回复内容或不回复，不等待人工批准。' },
};
const { Text } = Typography;
export function CommentsPage() {
  const isPageVisible = usePageVisible();
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [accountId, setAccountId] = useState<number>();
  const [data, setData] = useState<CommentMonitoring>();
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [retry, setRetry] = useState(0);
  const [saving, setSaving] = useState(false);
  const [agentId, setAgentId] = useState('');
  const [automatic, setAutomatic] = useState(true);
  const [controlMode, setControlMode] = useState<DmControlMode>('full_access');
  const [dirty, setDirty] = useState(false);
  const [routes, setRoutes] = useState<Array<{ id: string; name: string; route_id: string }>>([]);
  const [routesError, setRoutesError] = useState('');
  const activeAccount = useRef(accountId); activeAccount.current = accountId;
  const requestVersion = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    void fetchAccounts('xhs').then(items => {
      if (controller.signal.aborted) return;
      const pc = items.filter(a => a.sub_type === 'pc'); setAccounts(pc);
      let saved: number | undefined;
      try { saved = readAccountSelection('xhs_comment_account_id'); } catch { /* Browser storage may be unavailable. */ }
      setAccountId(pc.find(a => a.id === saved)?.id ?? pc.find(a => a.nickname?.includes('小桃'))?.id ?? pc[0]?.id);
    }).catch(() => { if (!controller.signal.aborted) setError('账号列表加载失败，请刷新页面重试。'); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    setData(undefined); setDirty(false); setAgentId(''); setRoutes([]); setRoutesError(''); if (!accountId) return;
    const controller = new AbortController(); let pending = false;
    async function refresh() {
      if (!isPageVisible() || pending) return; pending = true; setBusy(true); const version = ++requestVersion.current;
      try {
        const result = await fetchCommentMonitoring(accountId!, controller.signal);
        if (!controller.signal.aborted && version === requestVersion.current) { setData(result); setError(''); }
      } catch { if (!controller.signal.aborted) setError('评论状态加载失败，请重试。'); }
      finally { pending = false; if (!controller.signal.aborted) setBusy(false); }
    }
    void refresh(); const timer = setInterval(() => void refresh(), 5000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [accountId, retry]);
  useEffect(() => {
    if (data && !dirty && !saving) {
      setAutomatic(data.auto_reply ?? true); setControlMode(data.control_mode ?? 'full_access'); setAgentId(current => data.agent_id || current);
    }
  }, [data, dirty, saving]);
  useEffect(() => {
    if (!accountId || !data?.configured) return;
    const controller = new AbortController();
    void fetchCommentRoutes(accountId, controller.signal).then(result => {
      if (!controller.signal.aborted) { setRoutes(result.agents); if (!dirty && !data?.agent_id && result.default_agent_id) setAgentId(result.default_agent_id); setRoutesError(result.connected ? '' : 'Agent 列表未连接，请稍后刷新。'); }
    }).catch(() => { if (!controller.signal.aborted) setRoutesError('回复路由加载失败，请刷新重试；仍可停止监听。'); });
    return () => controller.abort();
  }, [accountId, data?.configured, retry]);
  async function configure(enabled: boolean) {
    if (!accountId) return;
    const id = accountId; ++requestVersion.current; setSaving(true); setError('');
    try {
      const result = await configureCommentListener(id, { enabled, auto_reply: automatic, control_mode: controlMode, agent_id: agentId });
      ++requestVersion.current;
      if (activeAccount.current === id) { setData(result); setDirty(false); }
    } catch { if (activeAccount.current === id) setError('评论配置保存失败，请检查账号和回复路由后重试。'); }
    finally { setSaving(false); }
  }
  async function act(row: CommentMonitoring['items'][number], approve: boolean) {
    if (!accountId) return;
    const id = accountId; ++requestVersion.current; setSaving(true); setError('');
    try {
      if (approve) await approveCommentReply(id, row.id, row.reply_version);
      else await skipCommentReply(id, row.id);
      const result = await fetchCommentMonitoring(id); ++requestVersion.current;
      if (activeAccount.current === id) setData(result);
    } catch { if (activeAccount.current === id) setError('操作未完成，正文或状态可能已变化，请刷新后重试。'); }
    finally { setSaving(false); }
  }
  const health = healthStates[data?.state ?? 'starting'] ?? ['状态待核对', 'orange'];
  return <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
    <PageHeader eyebrow="XHS Comments" title="评论监听" description="监听自己笔记下的评论，以及其他笔记下对我们的回复。每条留言都能查看处理进度和回复结果。" action={<Button icon={<ReloadOutlined />} loading={busy} onClick={() => setRetry(n => n + 1)}>刷新</Button>} />
    {error && <Alert type="error" showIcon title={error} />}
    <Row gutter={[16, 16]}><Col xs={24} lg={10}><Card title="监听账号"><Space wrap><Select aria-label="监听账号" disabled={saving} style={{ minWidth: 220 }} value={accountId} onChange={id => { setAccountId(id); try { saveAccountSelection('xhs_comment_account_id', id); } catch { /* Keep the selection for this visit. */ } }} options={accounts.map(a => ({ value: a.id, label: a.nickname || `账号 #${a.id}` }))} />
      {data?.configured && <><Tag color={health[1]}>{health[0]}</Tag><Tag>{data.auto_reply ? '回复发送已启用' : '仅生成回复'}</Tag><Tag>{controlModes[data.control_mode ?? 'full_access'].label}</Tag><Text type="secondary">每 {data.poll_seconds} 秒检查通知 · 页面每 5 秒刷新</Text></>}
    </Space>
    {data?.configured ? <Space direction="vertical" style={{ display: 'flex', marginTop: 16 }}>
      <Text>最近检查：{data.checked_at ? formatShanghaiTime(data.checked_at) : '尚未完成'} · 待处理 {data.pending ?? 0} 条 · 核验原帖可见 {data.verified_visible ?? 0} 条</Text>
      <Text type="secondary">提交成功后会用独立账号检查原帖。自己看得到、接收方看不到的回复会标记异常并继续只读复查；新评论按已应用的回复方式处理，已提交的回复不会重复发送。</Text>
      {data.account_problem && <Alert type="error" title={data.account_status === 'expired' ? '网页登录已过期，评论监听暂时无法工作' : '账号需要恢复'} description={<Space direction="vertical"><Text>{data.account_problem}</Text><Link to="/platforms/xhs/accounts">前往账号矩阵恢复登录</Link></Space>} showIcon />}
      {data.sending_paused && <Alert type="error" title="自动评论发送已暂停，通知监听仍在运行" description={data.sending_pause_reason} showIcon />}
      {data.error && <Alert type="error" title={`通知读取失败（${data.error}），后台正在等待恢复。`} showIcon />}
      {data.state === 'stale' && <Alert type="warning" title="后台状态未及时更新，请检查任务或网络。页面保留上次处理记录。" showIcon />}
      {data.state === 'paused' && <Alert type="info" title="自动化已暂停；恢复后继续处理队列。" />}
      {data.state === 'stopped' && <Alert type="info" title="评论监听已停止" description="暂停接收新评论和处理回复队列。重新开启后补收期间的留言；已经开始发送的请求可能完成，已提交回复继续核验。" />}
    </Space> : data && <Empty style={{ margin: 24 }} description="此账号暂无可用的评论接入配置，请先检查账号登录和消息中枢接入。" />}
    </Card></Col>
    {data?.configured && <Col xs={24} lg={14}><Card title="回复方式" extra={<Link to="/models">模型配置</Link>}>
      <Select aria-label="评论回复 Agent" disabled={saving} placeholder="选择当前账号 Agent" value={agentId || undefined} style={{ width: '100%', marginBottom: 16 }} onChange={value => { setAgentId(value); setDirty(true); }} options={routes.map(r => ({ value: r.id, label: r.name }))} />
      {routesError && <Alert type="warning" title={routesError} style={{ marginBottom: 12 }} />}
      <Typography.Paragraph strong style={{ marginBottom: 8 }}>控制方式</Typography.Paragraph>
      <Select aria-label="评论控制方式" disabled={saving} value={controlMode} onChange={value => { setControlMode(value); setDirty(true); }} style={{ width: '100%', marginBottom: 8 }} options={Object.entries(controlModes).map(([value, item]) => ({ value, label: item.label }))} />
      <Typography.Paragraph type="secondary">{controlModes[controlMode].description}</Typography.Paragraph>
      <Space wrap style={{ marginBottom: 16 }}><Switch checked={automatic} disabled={saving} onChange={value => { setAutomatic(value); setDirty(true); }} aria-label="评论回复发送" /><Text>{automatic ? controlMode === 'request_approval' ? '批准后发送给原评论用户' : '自动发送给原评论用户' : '仅生成回复，不发送'}</Text></Space>
      <div><Space wrap><Button type="primary" loading={saving} disabled={!agentId || !routes.some(r => r.id === agentId)} onClick={() => void configure(true)}>{data.enabled ? '应用回复配置' : '开始监听'}</Button><Button loading={saving} disabled={!data.enabled} onClick={() => void configure(false)}>停止监听</Button>{dirty && <Text type="secondary">有未应用的修改</Text>}</Space></div>
    </Card></Col>}</Row>
    {data?.configured && <>
      <Card title="评论与回复记录" extra={<Text type="secondary">最近 100 条 · 仅提交成功不算核验可见</Text>}>
        <Table rowKey="id" dataSource={data.items} pagination={{ pageSize: 10 }} scroll={{ x: 950 }} columns={[
          { title: '执行 Agent', width: 180, render: (_, row) => row.agent_snapshot?.name || '历史未记录' },
          { title: '收到的评论', width: 260, render: (_, row) => <Space direction="vertical"><Text>{row.text || '（空评论）'}</Text><Text type="secondary">{row.kind === 'own_note_comment' ? '自己笔记下的留言' : '其他笔记下回复我们'}</Text>{row.target_text && <Text type="secondary">回复了：{row.target_text}</Text>}<a href={`https://www.xiaohongshu.com/explore/${row.note_id}`} target="_blank" rel="noopener noreferrer">{row.note_title || '查看原笔记'}</a></Space> },
          { title: '状态', width: 145, render: (_, row) => <Tag color={states[row.stage]?.[1]}>{states[row.stage]?.[0] || row.stage}</Tag> },
          { title: '提交的回复及核验结果', width: 350, render: (_, row) => <Space direction="vertical"><Text>{row.reply_text || '—'}</Text>{row.reason && <Text type="secondary">{row.reason}</Text>}{row.detail && <Text type={row.stage === 'not_visible' ? 'danger' : 'secondary'}>{row.detail}</Text>}{row.verification?.checked_at && <Text type="secondary">核验时间：{formatShanghaiTime(row.verification.checked_at)}</Text>}{row.stage === 'thinking' && <Text type="secondary">Agent 正在处理，尚未发送。</Text>}</Space> },
          { title: '更新时间', width: 170, render: (_, row) => formatShanghaiTime(row.updated_at) },
          { title: '操作', width: 150, render: (_, row) => ['approval_pending', 'held', 'ready'].includes(row.stage) ? <Space wrap>
            {row.reply_text && row.stage !== 'ready' && <Popconfirm title="批准这条正文回复给原评论用户？" description={row.reply_text} onConfirm={() => act(row, true)}><Button size="small" disabled={saving}>批准发送</Button></Popconfirm>}
            <Popconfirm title="跳过这条回复？" onConfirm={() => act(row, false)}><Button size="small" disabled={saving}>不回复</Button></Popconfirm>
          </Space> : null },
        ]} />
      </Card>
    </>}
  </div>;
}
