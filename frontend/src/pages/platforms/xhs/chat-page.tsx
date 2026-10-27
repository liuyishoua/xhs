import { usePageVisible } from '../../../hooks/use-page-visible';
import { MessageOutlined, PictureOutlined, ReloadOutlined, SendOutlined, CloseOutlined } from '@ant-design/icons';
import { Alert, Avatar, Button, Card, Col, Empty, Image, Input, List, Modal, Row, Select, Space, Spin, Tag, Typography, Upload, theme } from 'antd';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { PageHeader } from '../../../components/layout/app-shell';
import { readAccountSelection, saveAccountSelection, fetchAccounts, fetchXhsChats, fetchXhsChatMessages, connectXhsChat, refreshXhsChat, sendXhsChat, checkXhsChatSend, uploadXhsChatImage, controlXhsChat, resolveXhsChatSend, fetchDmMedia } from '../../../lib/api';
import type { DmMediaRef, XhsChatConversation, XhsChatConnection, XhsChatMessage, XhsChatSend } from '../../../lib/api';
import type { PlatformAccount } from '../../../types';
import { formatShanghaiTime } from '../../../lib/time';

const status: Record<string, string> = { queued: '等待发送', sending: '发送中', sent: '已发送', failed: '发送失败', uncertain: '发送待核对', cancelled: '已取消' };
const connectionNames: Record<string, string> = { stopped: '未连接', stopping: '正在断开', connecting: '正在连接', syncing: '正在更新', listening: '已连接', reconnecting: '正在重连', network_paused: '连接暂停' };
function errorText(error: unknown, fallback: string): string {
  const value = error as { response?: { data?: { detail?: unknown } } };
  return typeof value.response?.data?.detail === 'string' ? value.response.data.detail : fallback;
}
function avatarUrl(value?: string) {
  try { const url = new URL(value || ''); return url.protocol === 'https:' && (url.hostname.endsWith('.xhscdn.com') || url.hostname.endsWith('.xiaohongshu.com')) ? value : undefined; } catch { return undefined; }
}
function ChatImage({ accountId, media }: { accountId: number; media: DmMediaRef }) {
  const target = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [url, setUrl] = useState('');
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const observer = new IntersectionObserver(entries => { if (entries.some(item => item.isIntersecting)) { setVisible(true); observer.disconnect(); } }, { rootMargin: '180px' });
    if (target.current) observer.observe(target.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController(); let local = '';
    void fetchDmMedia(accountId, media.asset_id, controller.signal).then(blob => {
      if (!controller.signal.aborted) { local = URL.createObjectURL(blob); setUrl(local); }
    }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => { controller.abort(); if (local) URL.revokeObjectURL(local); };
  }, [accountId, media.asset_id, visible]);
  return <div ref={target} style={{ minWidth: 120, minHeight: 100 }}>{url ? <Image src={url} alt={media.label} style={{ maxWidth: 220, maxHeight: 260, objectFit: 'contain' }} /> : failed ? <Typography.Text type="secondary">图片暂未加载</Typography.Text> : <Spin size="small" />}</div>;
}

export function XhsChatPage() {
  const isPageVisible = usePageVisible();
  const { token } = theme.useToken();
  const [params, setParams] = useSearchParams();
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [accountId, setAccountId] = useState<number>();
  const initialPeer = params.get('peer_id') || '';
  const [peerId, setPeerId] = useState(/^[A-Za-z0-9_-]{1,128}$/.test(initialPeer) ? initialPeer : '');
  const [conversations, setConversations] = useState<XhsChatConversation[]>([]);
  const [connection, setConnection] = useState<XhsChatConnection>();
  const [messages, setMessages] = useState<XhsChatMessage[]>([]);
  const [manual, setManual] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [search, setSearch] = useState('');
  const [text, setText] = useState('');
  const [asset, setAsset] = useState<DmMediaRef>();
  const [uploading, setUploading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refreshError, setRefreshError] = useState('');
  const [newChat, setNewChat] = useState(false);
  const [recipient, setRecipient] = useState('');
  const key = `${accountId}/${peerId}`;
  const active = useRef(key); active.current = key;
  const accountRef = useRef(accountId); accountRef.current = accountId;
  const drafts = useRef(new Map<string, string>());
  const attempts = useRef(new Map<string, XhsChatSend>());
  const imageRequest = useRef(0);
  const submitLock = useRef(false);
  const scroll = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const account = accounts.find(a => a.id === accountId);
  const peer = conversations.find(c => c.peer_id === peerId);
  const attempt = attempts.current.get(key);

  useEffect(() => {
    let live = true;
    void fetchAccounts().then(all => {
      if (!live) return;
      const pc = all.filter(a => a.sub_type === 'pc'); setAccounts(pc);
      let saved: number | undefined;
      try { saved = readAccountSelection('xhs_dm_account_id'); } catch { /* Optional persistence. */ }
      const requested = Number(params.get('account_id'));
      setAccountId(pc.find(a => a.id === requested)?.id || pc.find(a => a.id === saved)?.id || pc[0]?.id);
    }).catch(() => { if (live) setError('账号列表读取失败，请刷新页面。'); });
    return () => { live = false; };
    // The URL is used for the initial conversation; selection thereafter is local.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (!accountId) return;
    try { saveAccountSelection('xhs_dm_account_id', accountId); } catch { /* Optional persistence. */ }
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setConversations([]); setConnection(undefined);
    const update = async () => {
      if (!isPageVisible()) { timer = setTimeout(update, 1000); return; }
      try {
        const result = await fetchXhsChats(accountId, controller.signal);
        if (!controller.signal.aborted) { setConversations(result.items); setConnection(result.connection); setRefreshError(''); }
      } catch { if (!controller.signal.aborted) setRefreshError('会话列表更新失败，已保留上次记录。'); }
      if (!controller.signal.aborted) timer = setTimeout(update, 5000);
    };
    void update();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [accountId]);
  useLayoutEffect(() => {
    setMessages([]); setManual(false); setSyncing(false); setError(''); setNotice(''); setAsset(undefined);
    imageRequest.current += 1; setUploading(false);
    setText(drafts.current.get(key) || ''); stickToBottom.current = true;
  }, [key]);
  useEffect(() => {
    if (!accountId || !peerId) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    void refreshXhsChat(accountId, peerId).catch(err => { if (!controller.signal.aborted) setError(errorText(err, '聊天更新请求失败，请重试。')); });
    const update = async () => {
      if (!isPageVisible()) { timer = setTimeout(update, 1000); return; }
      try {
        const result = await fetchXhsChatMessages(accountId, peerId, controller.signal);
        if (!controller.signal.aborted) { setMessages(result.items); setManual(result.manual_control); setSyncing(result.syncing); }
        const pending = attempts.current.get(key);
        if (pending && !submitLock.current && !controller.signal.aborted) {
          try {
            await checkXhsChatSend(accountId, pending.client_id, controller.signal);
            if (!controller.signal.aborted && attempts.current.get(key) === pending) finalizeSubmission(key, pending);
          } catch { /* Keep the notice until the local submission is confirmed. */ }
        }
      } catch { if (!controller.signal.aborted) setError('聊天记录更新失败，已保留上次记录。'); }
      if (!controller.signal.aborted) timer = setTimeout(update, 3000);
    };
    void update();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [accountId, peerId, key]);
  useEffect(() => { if (stickToBottom.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; }, [messages]);

  function selectPeer(value: string) {
    setPeerId(value); setParams({ account_id: String(accountId), peer_id: value }, { replace: true });
  }
  async function updateChat(id = accountId, peer = peerId) {
    if (!id || !peer) return;
    const current = `${id}/${peer}`; const result = await fetchXhsChatMessages(id, peer);
    if (active.current === current) { setMessages(result.items); setManual(result.manual_control); setSyncing(result.syncing); }
  }
  function finalizeSubmission(context:string, request:XhsChatSend) {
    attempts.current.delete(context);
    if(!request.asset_id&&(drafts.current.get(context)||'').trim()===request.text){
      drafts.current.delete(context);if(active.current===context)setText('');
    }
    if(active.current===context){setAsset(undefined);setNotice('');stickToBottom.current=true;}
  }
  async function send() {
    if (!accountId || !peerId || !account?.external_user_id || sending || submitLock.current || uploading || (!text.trim() && !asset)) return;
    const context = key; const body = asset ? '' : text.trim();
    let request = attempts.current.get(context);
    if (request && (request.text !== body || request.asset_id !== asset?.asset_id)) { setNotice('上一条提交状态待核对，请先核对状态。'); return; }
    request ||= { client_id: crypto.randomUUID(), expected_external_user_id: account.external_user_id, peer_id: peerId, text: body, asset_id: asset?.asset_id };
    submitLock.current=true;attempts.current.set(context, request); setSending(true); setError(''); setNotice('');
    try {
      await sendXhsChat(accountId, request);
      finalizeSubmission(context,request);
      try { await updateChat(accountId, peerId); }
      catch { if (active.current === context) setRefreshError('消息已提交，聊天记录稍后更新。'); }
    } catch (err) {
      const code = (err as { response?: { status?: number } }).response?.status;
      if (code && code >= 400 && code < 500) {
        attempts.current.delete(context);
        if (active.current === context) setError(errorText(err, '消息未提交，请稍后重试。'));
      } else if (active.current === context) setNotice('提交状态待核对，正在更新发送记录；不会自动重发。');
    } finally { submitLock.current=false;setSending(false); }
  }
  async function checkAttempt() {
    if (!accountId || !attempt) return;
    const context=key;const id=accountId;const peer=peerId;
    try {
      await checkXhsChatSend(id, attempt.client_id);finalizeSubmission(context,attempt);
      if(active.current===context)setError('');
      try { await updateChat(id,peer); }
      catch { if (active.current === context) setRefreshError('提交状态已核对，聊天记录稍后更新。'); }
    } catch (err) { if(active.current===context)setNotice(errorText(err, '暂时无法核对，请稍后重试。')); }
  }
  async function upload(file: File) {
    if (!accountId) return false;
    const context = key;const request=++imageRequest.current; setUploading(true); setError('');
    try { const result = await uploadXhsChatImage(accountId, file); if (active.current === context&&imageRequest.current===request) setAsset(result); }
    catch (err) { if (active.current === context&&imageRequest.current===request) setError(errorText(err, '图片上传失败。')); }
    finally { if(imageRequest.current===request)setUploading(false); }
    return false;
  }
  async function toggleControl() {
    if (!accountId || !peerId) return;
    try { await controlXhsChat(accountId, peerId, !manual); await updateChat(); }
    catch (err) { setError(errorText(err, '会话控制更新失败。')); }
  }
  function openRecipient() {
    const value = recipient.trim(); const match = value.match(/\/user\/profile\/([a-f0-9]{24})/i);
    const id = match?.[1] || value;
    if (!/^[a-f0-9]{24}$/i.test(id)) { setError('请输入对方的小红书主页链接或用户 ID。'); return; }
    selectPeer(id); setNewChat(false); setRecipient('');
  }
  const filtered = conversations.filter(c => `${c.nickname} ${c.peer_id}`.toLowerCase().includes(search.toLowerCase()));
  return <div>
    <PageHeader eyebrow="XHS" title="私信聊天" description="选择账号，直接查看最近一天的聊天，发送文字、表情和图片。" />
    {error && <Alert type="error" showIcon title={error} style={{ marginBottom: 16 }} />}
    {notice && <Alert type="info" showIcon title={notice} action={attempt && <Button size="small" onClick={() => void checkAttempt()}>核对提交状态</Button>} style={{ marginBottom: 16 }} />}
    {refreshError && <Alert type="warning" showIcon title={refreshError} style={{ marginBottom: 16 }} />}
    <Card style={{ marginBottom: 16 }}><Space wrap>
      <Select aria-label="聊天账号" placeholder="选择小红书账号" value={accountId} style={{ minWidth: 240 }} options={accounts.map(a => ({ value: a.id, label: `${a.nickname || a.external_user_id} · PC` }))}
        onChange={id => { setAccountId(id); setPeerId(''); setSearch(''); setParams({ account_id: String(id) }, { replace: true }); }} />
      <Tag color={connection?.state === 'listening' ? 'green' : 'default'}>{connectionNames[connection?.state || 'stopped'] || '正在加载'}</Tag>
      <Button disabled={!accountId} onClick={() => void connectXhsChat(accountId!, !connection?.manual_enabled).then(() => fetchXhsChats(accountId!)).then(r => { if (accountRef.current === accountId) setConnection(r.connection); }).catch(err => setError(errorText(err, '连接更新失败。')))}>{connection?.manual_enabled ? '关闭手动连接' : '连接聊天'}</Button>
      <Button icon={<ReloadOutlined />} disabled={!accountId} onClick={() => void refreshXhsChat(accountId!).catch(err => setError(errorText(err, '会话更新失败。')))}>刷新会话</Button>
      {connection?.auto_reply && <Tag>自动回复已开启</Tag>}<Link to="/platforms/xhs/messages">自动回复设置</Link>
    </Space>{connection?.error && <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>{connection.error}</Typography.Paragraph>}</Card>
    <Row gutter={[16, 16]}>
      <Col xs={24} lg={7}><Card title={<Space><MessageOutlined />会话</Space>} extra={<Button size="small" disabled={!accountId} onClick={() => setNewChat(true)}>新会话</Button>}>
        <Input aria-label="搜索会话" allowClear placeholder="搜索昵称或用户 ID" value={search} onChange={e => setSearch(e.target.value)} style={{ marginBottom: 12 }} />
        <div style={{ maxHeight: 590, overflowY: 'auto' }}><List dataSource={filtered} locale={{ emptyText: '暂无会话，点击“刷新会话”加载。' }} renderItem={item => <List.Item onClick={() => selectPeer(item.peer_id)} tabIndex={0} role="button" aria-label={`与${item.nickname}聊天`} onKeyDown={e => { if (e.key === 'Enter') selectPeer(item.peer_id); }} style={{ cursor: 'pointer', background: peerId === item.peer_id ? token.colorPrimaryBg : undefined, borderRadius: 8, padding: 12 }}>
          <List.Item.Meta avatar={<Avatar src={avatarUrl(item.avatar)}>{item.nickname.slice(0, 1)}</Avatar>} title={<Typography.Text ellipsis style={{ maxWidth: '100%' }}>{item.nickname}</Typography.Text>}
            description={<><Typography.Paragraph type="secondary" ellipsis style={{ margin: 0 }}>{item.last_text || '查看聊天'}</Typography.Paragraph>{item.last_at && <Typography.Text type="secondary" style={{ fontSize: 11 }}>{formatShanghaiTime(item.last_at)}</Typography.Text>}</>} />
        </List.Item>} /></div>
      </Card></Col>
      <Col xs={24} lg={17}><Card title={peerId ? <Space wrap><span>{peer?.nickname || peerId}</span><Tag>{manual ? '手动接管中' : connection?.auto_reply ? '自动回复中' : '手动聊天'}</Tag>{syncing && <Tag color="blue">正在增量更新</Tag>}</Space> : '聊天'}
        extra={peerId && <Space><Button size="small" onClick={() => void refreshXhsChat(accountId!, peerId).catch(err => setError(errorText(err, '聊天更新失败。')))}>同步当前聊天</Button><Button size="small" onClick={() => void toggleControl()}>{manual ? connection?.auto_reply ? '恢复自动回复' : '结束手动接管' : '手动接管'}</Button></Space>}>
        {!peerId ? <Empty description="选择左侧会话开始聊天" /> : <>
          <div ref={scroll} aria-label="聊天记录" onScroll={() => { if (scroll.current) stickToBottom.current = scroll.current.scrollHeight - scroll.current.scrollTop - scroll.current.clientHeight < 100; }} style={{ height: 'min(55vh, 540px)', minHeight: 260, overflowY: 'auto', padding: '8px 4px 16px' }}>
            {!messages.length && <Empty description={syncing ? '正在读取最近一天的聊天' : '最近一天暂无聊天记录'} />}
            {messages.map(item => <div key={item.key} style={{ display: 'flex', flexDirection: 'column', alignItems: item.direction === 'outbound' ? 'flex-end' : 'flex-start', marginBottom: 16 }}>
              <Typography.Text type="secondary" style={{ fontSize: 11, marginBottom: 4 }}>{item.direction === 'outbound' ? account?.nickname || '我' : peer?.nickname || '对方'} · {formatShanghaiTime(item.sent_at)}</Typography.Text>
              <div style={{ maxWidth: '85%', padding: '10px 14px', borderRadius: 12, color:token.colorText, background: item.direction === 'outbound' ? token.colorPrimaryBg : token.colorFillTertiary, overflowWrap: 'anywhere' }}>
                {item.revoked ? <Typography.Text type="secondary">消息已撤回</Typography.Text> : item.media?.length ? item.media.map(media => <ChatImage key={media.asset_id} accountId={accountId!} media={media} />) : <span style={{ whiteSpace: 'pre-wrap' }}>{item.text}</span>}
              </div>
              {item.direction === 'outbound' && <Space wrap style={{ marginTop: 4 }}><Typography.Text type={item.state === 'failed' || item.state === 'uncertain' ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>{status[item.state] || item.state}</Typography.Text>
                {item.error && <Typography.Text type="secondary" style={{ fontSize: 12 }}>{item.error}</Typography.Text>}
                {item.manual_id && ['uncertain', 'queued'].includes(item.state) && <Button size="small" onClick={() => void resolveXhsChatSend(item.manual_id!, 'skip').then(() => updateChat()).catch(err => setError(errorText(err, '状态已变化，请刷新。')))}>{item.state === 'queued' ? '取消发送' : '跳过并继续'}</Button>}
                {item.manual_id && item.state === 'uncertain' && <Button size="small" onClick={() => void resolveXhsChatSend(item.manual_id!, 'confirmed_sent').then(() => updateChat()).catch(err => setError(errorText(err, '核对失败。')))}>确认对方收到</Button>}
                {item.manual_id&&item.state==='failed'&&<Button size="small" onClick={()=>{const body=item.media?.length?'':item.text;setText(body);drafts.current.set(key,body);setAsset(item.media?.[0]);}}>重新编辑</Button>}
              </Space>}
            </div>)}
          </div>
          {asset && <Space style={{ marginBottom: 8 }}><Tag icon={<PictureOutlined />}>已选择图片</Tag><Button size="small" icon={<CloseOutlined />} onClick={() => setAsset(undefined)}>移除</Button></Space>}
          <Input.TextArea aria-label="发送消息" placeholder={asset ? '本次发送图片，文字可在下一条发送' : '输入消息，Enter 发送，Shift + Enter 换行'} value={text} disabled={Boolean(asset)} maxLength={2000} autoSize={{ minRows: 3, maxRows: 6 }}
            onChange={e => { setText(e.target.value); drafts.current.set(key, e.target.value); }} onKeyDown={e => {
              if (e.key !== 'Enter' || e.shiftKey || e.altKey || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
              e.preventDefault();
              if (!e.repeat) void send();
            }} />
          <Space wrap style={{ marginTop: 12 }}>
            <Upload accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" beforeUpload={upload} showUploadList={false}><Button icon={<PictureOutlined />} loading={uploading} disabled={sending}>图片</Button></Upload>
            {['😊', '👍', '❤️'].map(emoji => <Button key={emoji} size="small" disabled={Boolean(asset)} onClick={() => { const value = text + emoji; setText(value); drafts.current.set(key, value); }}>{emoji}</Button>)}
            <Button type="primary" icon={<SendOutlined />} loading={sending} disabled={uploading || (!text.trim() && !asset) || !account?.external_user_id} onClick={() => void send()}>发送</Button>
          </Space>
          <Typography.Paragraph type="secondary" style={{ marginTop: 10, marginBottom: 0, fontSize: 12 }}>手动发送不会开启接管。需要暂停此会话的自动回复时，点击“手动接管”；点击“恢复自动回复”可交回模型。连接中断时，已提交的消息等待恢复后发送，可取消等待中的消息。</Typography.Paragraph>
        </>}
      </Card></Col>
    </Row>
    <Modal title="开始新会话" open={newChat} onCancel={() => setNewChat(false)} onOk={openRecipient} okText="打开聊天"><Input aria-label="对方主页链接或用户ID" placeholder="粘贴对方的小红书主页链接或用户 ID" value={recipient} onChange={e => setRecipient(e.target.value)} onPressEnter={openRecipient} /></Modal>
  </div>;
}
