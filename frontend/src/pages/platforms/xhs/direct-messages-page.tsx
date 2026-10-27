import { usePageVisible } from '../../../hooks/use-page-visible';
import { CloudOutlined, MessageOutlined, ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Col, Drawer, Empty, Popconfirm, Row, Select, Space, Spin, Switch, Table, Tag, Typography, Image } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader } from '../../../components/layout/app-shell';
import { readAccountSelection, saveAccountSelection, fetchDmAgents, fetchDmMedia, approveDirectMessage, checkXhsNetwork, configureDmListener, fetchAccounts, fetchDirectMessages, fetchDmHistory, fetchDmListeners, fetchXhsNetwork, reanalyzeDirectMessage, resolveDirectMessage } from '../../../lib/api';
import type { AgentChoice, DmMediaRef, DirectMessageRecord, DmControlMode, DmHistoryResult, DmListener, XhsNetworkStatus } from '../../../lib/api';
import type { PlatformAccount } from '../../../types';
import { formatShanghaiTime } from '../../../lib/time';

const states: Record<string, [string, string]> = {
  history_pending: ['同步一天聊天', 'blue'], history_error: ['历史同步重试中', 'orange'],
  approval_pending: ['待批准发送', 'gold'],
  queued: ['待分析', 'default'], waiting: ['正在判断回复', 'blue'], ready: ['待发送', 'cyan'],
  sending: ['发送中', 'blue'], sent: ['平台已接收', 'blue'], uncertain: ['发送待核对', 'orange'],
  held: ['特殊事项待处理', 'orange'], failed: ['失败', 'red'], skipped: ['不回复', 'default'], superseded: ['已合并后续消息', 'default'],
};
const connections: Record<string, string> = { stopped: '已停止', stopping: '正在停止', connecting: '正在连接', syncing: '正在补收断线消息', listening: '正在监听', reconnecting: '正在重连', network_paused: '家庭网络断开 · 已暂停' };
const networkStates: Record<XhsNetworkStatus['state'], [string, string]> = {
  direct: ['服务器出口', 'orange'], checking: ['正在检测', 'blue'], online: ['已连通', 'green'],
  offline: ['连接断开', 'red'], restricted: ['平台限制', 'red'], unconfigured: ['待配置', 'orange'],
};
const controlModes: Record<DmControlMode, { label: string; description: string }> = {
  request_approval: { label: '请求批准', description: '模型生成最终回复，每条正文经你批准后发送。' },
  auto_approve: { label: '帮我批准', description: '模型自动处理普通聊天，特殊事项才交给你。' },
  full_access: { label: '完全访问', description: '模型自主决定回复内容或不回复，不等待人工批准。' },
};

function MediaPreview({ accountId, media }: { accountId: number; media: DmMediaRef }) {
  const [url, setUrl] = useState('');
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true; let objectUrl = '';
    const controller = new AbortController();
    setUrl(''); setFailed(false);
    void fetchDmMedia(accountId, media.asset_id, controller.signal).then(blob => {
      if (active) { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl); }
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [accountId, media.asset_id, attempt]);
  return <div style={{ marginTop: 6 }}>{url ? <Image src={url} alt={media.label} style={{ maxWidth: '100%', maxHeight: 160, objectFit: 'contain' }} width={144} /> : failed ? <Button size="small" onClick={() => setAttempt(attempt + 1)}>图片加载失败 · 重试</Button> : <Spin size="small" />}<div><Tag>{media.kind === 'image' ? '图片' : media.kind === 'sticker' ? '表情包' : '表情'}</Tag></div></div>;
}

export function DirectMessagesPage() {
  const isPageVisible = usePageVisible();
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [listeners, setListeners] = useState<DmListener[]>([]);
  const [network, setNetwork] = useState<XhsNetworkStatus>();
  const [networkBusy, setNetworkBusy] = useState(false);
  const [accountId, setAccountId] = useState<number>();
  const activeAccount = useRef(accountId);
  activeAccount.current = accountId;
  const [agentId, setAgentId] = useState('');
  const [agents, setAgents] = useState<AgentChoice[]>([]);
  const [agentsConnected, setAgentsConnected] = useState(false);
  const [automatic, setAutomatic] = useState(false);
  const [controlMode, setControlMode] = useState<DmControlMode>('auto_approve');
  const [rows, setRows] = useState<DirectMessageRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [refreshError, setRefreshError] = useState('');
  const refreshRequest = useRef(0);
  const [historyPeer, setHistoryPeer] = useState<string>();
  const [chatHistory, setChatHistory] = useState<DmHistoryResult>();
  const [historyBusy, setHistoryBusy] = useState(false);
  const historyRequest = useRef(0);
  const listener = listeners.find(item => item.account_id === accountId);
  const waitingForReconnect = listener?.state === 'network_paused' && network?.state === 'online';

  async function refresh(id = accountId, signal?: AbortSignal, reloadAgents = false) {
    const request = ++refreshRequest.current;
    const [current, currentNetwork, messages, directory] = await Promise.allSettled([
      fetchDmListeners(signal), fetchXhsNetwork(signal),
      id ? fetchDirectMessages(id, signal) : Promise.resolve({ items: [] as DirectMessageRecord[] }),
      reloadAgents && id ? fetchDmAgents(id, signal) : Promise.resolve(null),
    ]);
    if (signal?.aborted || activeAccount.current !== id || request !== refreshRequest.current) return;
    const failed: string[] = [];
    if (current.status === 'fulfilled') setListeners(current.value.items);
    else failed.push('监听状态');
    if (currentNetwork.status === 'fulfilled') setNetwork(currentNetwork.value);
    else failed.push('网络状态');
    if (messages.status === 'fulfilled') { if (id) setRows(messages.value.items); }
    else failed.push('消息记录');
    if (directory.status === 'fulfilled' && directory.value) { setAgents(directory.value.agents); setAgentsConnected(directory.value.connected); }
    else if (directory.status === 'rejected') { setAgents([]); setAgentsConnected(false); failed.push('Agent 目录'); }
    setRefreshError(failed.length ? `${failed.join('、')}刷新失败，正在自动重试；相关内容保留上次结果。` : '');
  }
  useEffect(() => {
    void Promise.all([fetchAccounts(), fetchDmListeners(), fetchXhsNetwork()]).then(([all, current, currentNetwork]) => {
      const pc = all.filter(a => a.sub_type === 'pc');
      let saved: number | undefined;
      try { saved = readAccountSelection('xhs_dm_account_id'); } catch { /* Browser storage may be unavailable. */ }
      setAccounts(pc); setListeners(current.items);
      setAccountId(selected => pc.find(a => a.id === selected)?.id || pc.find(a => a.id === saved)?.id
        || pc.find(a => current.items.some(item => item.account_id === a.id && item.enabled))?.id || pc[0]?.id);
      setNetwork(currentNetwork);
    }).catch(() => setError('私信监听信息加载失败。'));
  }, []);
  useEffect(() => {
    if (accountId && accounts.some(a => a.id === accountId)) {
      try { saveAccountSelection('xhs_dm_account_id', accountId); } catch { /* Keep the selection for this visit. */ }
    }
  }, [accountId, accounts]);
  useEffect(() => {
    const controller = new AbortController();
    setRows([]); setRefreshError('');
    setHistoryPeer(undefined); setChatHistory(undefined); historyRequest.current += 1;
    const current = listeners.find(item => item.account_id === accountId);
    setAgentId(current?.agent_id || ''); setAutomatic(current?.auto_reply || false);
    setControlMode(current?.control_mode || 'auto_approve');
    if (!accountId) return () => controller.abort();
    setAgents([]); setAgentsConnected(false);
    void fetchDmAgents(accountId, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      setAgents(result.agents); setAgentsConnected(result.connected);
      if (!current?.agent_id) setAgentId(result.agents.find(a => a.route_id === current?.route_id)?.id || result.default_agent_id || '');
    }).catch(() => { if (!controller.signal.aborted) setError('当前账号 Agent 列表读取失败，请刷新。'); });
    let timer: ReturnType<typeof setTimeout>;
    const update = async () => {
      if (!isPageVisible()) { timer = setTimeout(update, 1000); return; }
      await refresh(accountId, controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(update, 5000);
    };
    void update();
    return () => { controller.abort(); clearTimeout(timer); };
    // Keep unsaved controls stable while status polling refreshes the records.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId]);
  async function configure(enabled: boolean) {
    if (!accountId) return;
    setBusy(true); setError('');
    try {
      await configureDmListener(accountId, { enabled, auto_reply: automatic, agent_id: agentId, control_mode: controlMode });
      await refresh();
    } catch { setError('操作失败，请检查 PC 账号和私信回复 Agent。'); }
    finally { setBusy(false); }
  }
  async function inspectNetwork() {
    setNetworkBusy(true);
    try { setNetwork(await checkXhsNetwork()); }
    catch { setError('网络检测失败，请稍后重试。'); }
    finally { setNetworkBusy(false); }
  }
  async function resolve(id: number, outcome: 'confirmed_sent' | 'skip') {
    setBusy(true);
    try { await resolveDirectMessage(id, outcome); await refresh(); }
    catch { setError('处理失败，消息状态可能已变化，请刷新。'); }
    finally { setBusy(false); }
  }
  async function reanalyze(id: number) {
    setBusy(true);
    try { await reanalyzeDirectMessage(id); await refresh(); }
    catch { setError('重新分析失败，请刷新消息状态。'); }
    finally { setBusy(false); }
  }
  async function approve(id: number) {
    setBusy(true);
    try { await approveDirectMessage(id); await refresh(); }
    catch { setError('批准失败，可能已有更新消息，请刷新记录。'); }
    finally { setBusy(false); }
  }
  async function showHistory(peer: string) {
    if (!accountId) return;
    const request = ++historyRequest.current;
    setHistoryPeer(peer); setChatHistory(undefined); setHistoryBusy(true);
    try {
      const result = await fetchDmHistory(accountId, peer);
      if (request === historyRequest.current) setChatHistory(result);
    } catch { if (request === historyRequest.current) setError('聊天历史加载失败。'); }
    finally { if (request === historyRequest.current) setHistoryBusy(false); }
  }
  return <div>
    <PageHeader eyebrow="XHS · Channel · Multica" title="私信监听" description="读取文字、表情包和图片，结合聊天历史自主选择回复内容与形式。"
      action={<Button icon={<ReloadOutlined />} onClick={() => void refresh(accountId, undefined, true).catch(() => setError('刷新失败。'))}>刷新记录</Button>} />
    {error && <Alert type="error" showIcon title={error} style={{ marginBottom: 16 }} />}
    {refreshError && <Alert type="warning" showIcon title={refreshError} style={{ marginBottom: 16 }} />}
    <Card title={<Space><CloudOutlined />小红书 PC 网络出口</Space>} style={{ marginBottom: 20 }}
      extra={<Button loading={networkBusy} onClick={() => void inspectNetwork()}>检测连接</Button>}>
      <Space wrap style={{ marginBottom: 12 }}>
        <Typography.Text strong>{network?.label || '正在加载'}</Typography.Text>
        {network && <Tag color={networkStates[network.state][1]}>{networkStates[network.state][0]}</Tag>}
        {network?.mode === 'home' && <Tag>固定家庭出口</Tag>}
      </Space>
      <Typography.Paragraph style={{ marginBottom: 8 }}>{network?.message || '正在读取连接状态'}</Typography.Paragraph>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
        PC 账号登录、笔记读取和私信共用此出口。家庭连接断开时暂停处理，恢复后自动重连。
      </Typography.Paragraph>
      <Space wrap size="large">
        {network?.checked_at && <Typography.Text type="secondary">最近检测：{formatShanghaiTime(network.checked_at)}</Typography.Text>}
        {network?.exit_ip && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>出口 IP：{network.exit_ip}</Typography.Text>}
      </Space>
      {network?.authorization_expires_at && <Alert type="warning" showIcon style={{ marginTop: 12 }}
        title={`家庭连接仍使用临时授权，到期时间：${formatShanghaiTime(network.authorization_expires_at)}`}
        description="请在 Mac mini 完成长期授权，避免之后重新连接失败。" />}
    </Card>
    {!accounts.length ? <Card><Empty description={<>还没有小红书 PC 账号，请先到 <Link to="/platforms/xhs/accounts">账号矩阵</Link> 绑定。</>} /></Card> : <>
      {Boolean(listener?.conversation_holds?.length) && <Alert type="warning" showIcon style={{ marginBottom: 20 }}
        title={`${listener?.conversation_holds?.length} 个会话需要处理，后续自动回复已暂停`}
        description={<Space orientation="vertical" style={{ width: '100%' }}>{listener?.conversation_holds?.map(hold => <div key={hold.message_id}>
          <Typography.Paragraph style={{ marginBottom: 8 }}><Typography.Text copyable>{hold.peer_id}</Typography.Text>：{hold.reason || '上一条回复需要人工处理'}</Typography.Paragraph>
          <Space wrap>
            {hold.state === 'uncertain' && <Popconfirm title="已在接收方确认旧回复可见？" onConfirm={() => resolve(hold.message_id, 'confirmed_sent')}><Button size="small" disabled={busy}>确认对方已收到</Button></Popconfirm>}
            <Popconfirm title="跳过旧回复并恢复该会话后续处理？" onConfirm={() => resolve(hold.message_id, 'skip')}><Button size="small" disabled={busy}>跳过旧回复并恢复</Button></Popconfirm>
          </Space>
        </div>)}</Space>} />}
      <Row gutter={[20, 20]} style={{ marginBottom: 20 }}>
        <Col xs={24} lg={10}><Card title={<Space><MessageOutlined />监听账号</Space>}>
          <Select aria-label="监听账号" style={{ width: '100%', marginBottom: 16 }} value={accountId} options={accounts.map(a => ({ value: a.id, label: `${a.nickname || a.external_user_id} · PC` }))} onChange={setAccountId} />
          <Space wrap><Tag color={listener?.state === 'listening' ? 'green' : 'default'}>{waitingForReconnect ? '家庭网络已恢复 · 等待监听重连' : connections[listener?.state || 'stopped'] || listener?.state}</Tag>
            {listener?.enabled && <><Tag color={listener.auto_reply ? 'blue' : 'default'}>{listener.auto_reply ? '回复发送已启用' : '仅生成回复'}</Tag><Tag>{controlModes[listener.control_mode || 'auto_approve'].label}</Tag></>}
          </Space>
          {waitingForReconnect ? <Alert type="info" title="家庭网络已恢复，监听连接正在重试；恢复监听后才处理消息。" style={{ marginTop: 16 }} /> : listener?.error && <Alert type="warning" title={listener.error} style={{ marginTop: 16 }} />}
        </Card></Col>
        <Col xs={24} lg={14}><Card title="回复方式" extra={<Link to="/models">模型配置</Link>}>
          <Select aria-label="回复 Agent" placeholder="选择当前账号 Agent" value={agentId || undefined} style={{ width: '100%', marginBottom: 16 }} onChange={setAgentId}
            options={agents.map(a => ({ value: a.id, label: a.name }))} />
          <Typography.Paragraph strong style={{ marginBottom: 8 }}>控制方式</Typography.Paragraph>
          <Select aria-label="控制方式" value={controlMode} onChange={setControlMode} style={{ width: '100%', marginBottom: 8 }} options={Object.entries(controlModes).map(([value, item]) => ({ value, label: item.label }))} />
          <Typography.Paragraph type="secondary">{controlModes[controlMode].description}</Typography.Paragraph>
          <Space wrap style={{ marginBottom: 16 }}><Switch checked={automatic} onChange={setAutomatic} aria-label="自动回复" /><Typography.Text>{controlMode === 'request_approval' ? '批准后发送给原私信用户' : '自动发送回复给原私信用户'}</Typography.Text></Space>
          <div><Space wrap>
            <Button type="primary" loading={busy} disabled={!agentId || !agentsConnected || !agents.some(a => a.id === agentId)} onClick={() => void configure(true)}>{listener?.enabled ? '应用回复配置' : '开始监听'}</Button>
            <Button loading={busy} disabled={!listener?.enabled} onClick={() => void configure(false)}>停止监听</Button>
          </Space></div>
        </Card></Col>
      </Row>
      <Alert type="info" showIcon title="结合最近一天聊天，自主判断是否回复" style={{ marginBottom: 20 }} description="收到一对一文字、表情或图片后，同步双方最近 24 小时的聊天，结合意图、语气和聊天进展生成回复。连续消息合并判断，模型可以选择不回复。按账号配置的控制方式发送，批准只对当前这条回复及素材有效。表情包和图片会以真实图像传给模型，动态图片采样首、中、末帧；每轮最多读取 8 份最近素材。历史消息只作为上下文；发送结果不确定时暂停该用户的后续回复，核对后继续。" />
      <Card title="消息与回复记录" extra={<Typography.Text type="secondary">每 5 秒刷新 · 最近 100 条</Typography.Text>}>
        <Typography.Paragraph type="secondary">“平台已接收”表示小红书返回成功回执，尚未确认对方手机显示或已读。</Typography.Paragraph>
        <Table<DirectMessageRecord> rowKey="id" dataSource={rows} scroll={{ x: 900 }} pagination={{ pageSize: 10 }} locale={{ emptyText: '暂无新私信。开始监听后，新消息会显示在这里。' }} columns={[
          { title: '执行 Agent', width: 180, render: (_, row) => row.agent_snapshot?.name || '历史未记录' },
          { title: '私信用户', dataIndex: 'peer_id', width: 160, render: (value, row) => <Space orientation="vertical"><Typography.Text copyable>{value}</Typography.Text><Link to={`/platforms/xhs/chat?account_id=${row.account_id}&peer_id=${encodeURIComponent(value)}`}>打开聊天</Link><Button size="small" onClick={() => void showHistory(value)}>查看一天聊天{Number.isFinite(row.history?.total_messages) ? ` · ${row.history?.total_messages} 条` : ''}</Button>{row.history?.context_truncated && <Tag>模型使用最近部分，完整记录已保存</Tag>}</Space> },
          { title: '收到的消息', dataIndex: 'text', width: 230, render: (value, row) => <div><Typography.Paragraph ellipsis={{ rows: 3, expandable: true }}>{value}</Typography.Paragraph>{row.media?.map(media => <MediaPreview key={media.asset_id} accountId={row.account_id} media={media} />)}</div> },
          { title: '模型回复', dataIndex: 'reply_text', width: 260, render: (value, row) => <div>{row.reply_messages?.length ? row.reply_messages.map((reply, index) => <div key={index} style={{ borderLeft: '2px solid var(--ui-line,#eee)', paddingLeft: 8, marginBottom: 8 }}><Typography.Text type="secondary">{index + 1} · {({ pending: '待发送', sending: '发送中', sent: '平台已接收', failed: '发送失败', uncertain: '待核对' } as Record<string, string>)[reply.status] || reply.status}{reply.status === 'pending' && ['superseded', 'skipped', 'sent', 'failed', 'uncertain'].includes(row.state) ? ' · 后续已停止' : ''}</Typography.Text>{!reply.media?.length && <Typography.Paragraph style={{ marginBottom: 4 }}>{reply.text}</Typography.Paragraph>}{reply.media?.map(media => <MediaPreview key={media.asset_id} accountId={row.account_id} media={media} />)}</div>) : <>{value ? <Typography.Paragraph ellipsis={{ rows: 3, expandable: true }}>{value}</Typography.Paragraph> : '—'}{row.reply_message?.media?.map(media => <MediaPreview key={media.asset_id} accountId={row.account_id} media={media} />)}</>}{row.analysis?.summary && <Typography.Paragraph type="secondary" ellipsis={{ rows: 2, expandable: true }}>{row.analysis.summary}</Typography.Paragraph>}</div> },
          { title: '状态', width: 180, render: (_, row) => <Space orientation="vertical"><Tag color={states[row.state]?.[1]}>{row.state === 'sent' && row.receipt.manually_resolved === 'confirmed_sent' ? '已人工核对送达' : states[row.state]?.[0] || row.state}</Tag>{row.receipt.network && <Tag>{row.receipt.network.label}</Tag>}{row.error && <Typography.Text type="secondary">{row.error}</Typography.Text>}</Space> },
          { title: '时间', dataIndex: 'created_at', width: 170, render: formatShanghaiTime },
          { title: '处理', width: 160, render: (_, row) => <Space orientation="vertical">
            {row.state === 'approval_pending' && <Button type="primary" size="small" disabled={busy} onClick={() => void approve(row.id)}>批准发送</Button>}
            {['held', 'failed', 'ready', 'history_error', 'approval_pending'].includes(row.state) && !row.reply_messages?.some(reply => reply.status === 'sent') && <Button size="small" disabled={busy} onClick={() => void reanalyze(row.id)}>重新分析</Button>}
            {row.state === 'uncertain' && <Popconfirm title="已在接收方确认这条回复可见？" onConfirm={() => resolve(row.id, 'confirmed_sent')}><Button size="small" disabled={busy}>确认对方已收到</Button></Popconfirm>}
            {['uncertain', 'held', 'ready', 'approval_pending'].includes(row.state) && <Popconfirm title="跳过这条回复并继续处理后续消息？" onConfirm={() => resolve(row.id, 'skip')}><Button size="small" disabled={busy}>跳过回复</Button></Popconfirm>}
            {row.channel_event_id && <Typography.Text copyable={{ text: row.channel_event_id }} type="secondary">Channel ID</Typography.Text>}
          </Space> },
        ]} />
      </Card>
    </>}
    <Drawer title="最近一天的双向聊天" open={Boolean(historyPeer)} onClose={() => { setHistoryPeer(undefined); historyRequest.current += 1; }} styles={{ wrapper: { width: 'min(560px, 100vw)' } }}>
      <Spin spinning={historyBusy}>
        <Typography.Paragraph type="secondary">{historyPeer}</Typography.Paragraph>
        {chatHistory?.analysis && <Card size="small" title="当前聊天判断" style={{ marginBottom: 20 }}>
          {([['intent', '对方意图'], ['style', '聊天风格'], ['relationship_stage', '聊天进展']] as const).map(([key, label]) => chatHistory.analysis[key] && <Typography.Paragraph key={key}><Typography.Text strong>{label}：</Typography.Text>{chatHistory.analysis[key]}</Typography.Paragraph>)}
        </Card>}
        {chatHistory?.history.synced_at && <Typography.Paragraph type="secondary">同步于 {formatShanghaiTime(chatHistory.history.synced_at)}</Typography.Paragraph>}
        {chatHistory?.items.map((item, index) => <div key={`${item.message_id}-${index}`} style={{ marginBottom: 16, textAlign: item.direction === 'outbound' ? 'right' : 'left' }}>
          <Typography.Text type="secondary">{item.direction === 'outbound' ? '我' : '对方'} · {formatShanghaiTime(item.sent_at)}</Typography.Text>
          <div style={{ padding: '10px 14px', marginTop: 4, borderRadius: 10, background: item.direction === 'outbound' ? 'rgba(22,119,255,.15)' : 'rgba(128,128,128,.12)', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', textAlign: 'left' }}>{item.text}{!item.revoked && accountId && item.media?.map(media => <MediaPreview key={media.asset_id} accountId={accountId} media={media} />)}</div>
        </div>)}
        {!historyBusy && !chatHistory?.items.length && <Empty description="尚未同步到聊天历史" />}
      </Spin>
    </Drawer>
  </div>;
}
