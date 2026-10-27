import { ApiOutlined, MessageOutlined, PictureOutlined, ReloadOutlined, RobotOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Col, Descriptions, Empty, Row, Select, Space, Spin, Steps, Tag, Typography } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader } from '../../components/layout/app-shell';
import { fetchAccounts, fetchModelRouting, saveModelRouting } from '../../lib/api';
import type { ModelRouting } from '../../lib/api';

const purposes = [
  { id: 'text', title: '内容创作', icon: <RobotOutlined />, detail: '笔记生成、改写、标题、标签和文案润色。' },
  { id: 'image', title: '图片任务', icon: <PictureOutlined />, detail: '图片分析与生成。生成图片需要 agent 具备图片生成能力；参考图请先上传到素材库。' },
  { id: 'dm', title: '私信回复', icon: <MessageOutlined />, detail: '为私信监听选择回复助手，再到私信监听页按账号启用。' },
];

export function ModelConfigPage() {
  const [accounts, setAccounts] = useState<Array<{ id: number; nickname: string }>>([]);
  const [accountId, setAccountId] = useState<number>();
  const activeAccount = useRef(accountId); activeAccount.current = accountId;
  const [data, setData] = useState<ModelRouting | null>(null);
  const [selected, setSelected] = useState<Record<string, string | null>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState('');
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  async function load() {
    const targetAccount = accountId;
    setLoading(true);
    try {
      const result = await fetchModelRouting(targetAccount);
      if (targetAccount !== activeAccount.current) return;
      setData(result); setSelected(Object.fromEntries(Object.entries(result.bindings).map(([purpose, route]) => [purpose, result.routes.find(r => r.id === route)?.agent?.id || null]))); setError('');
    } catch { if (targetAccount === activeAccount.current) setError('连接信息加载失败，请重试。'); }
    finally { if (targetAccount === activeAccount.current) setLoading(false); }
  }
  useEffect(() => { void fetchAccounts().then(all => { const pc = all.filter(a => a.platform === 'xhs' && a.sub_type === 'pc'); setAccounts(pc); setAccountId(pc[0]?.id); }); }, []);
  useEffect(() => { if (accountId) void load(); else setLoading(false); }, [accountId]);
  async function save(purpose: string) {
    const route = selected[purpose];
    if (!route || !accountId) return;
    setSaving(purpose); setError(''); setSuccess('');
    try {
      await saveModelRouting(purpose, route, accountId);
      await load(); setSuccess('Agent 已保存，新任务将使用对应的 Multica agent。');
    } catch { setError('保存失败，请刷新 Agent 列表后重试。'); }
    finally { setSaving(''); }
  }
  return <div>
    <PageHeader eyebrow="Multica" title="模型配置" description="按用途选择执行助手。模型和运行环境由 Multica 统一管理，Channel 负责路由与会话。"
      action={<Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>刷新连接</Button>} />
    <Select aria-label="执行账号" placeholder="选择小红书账号" value={accountId} disabled={!!saving} onChange={value => { setData(null); setSelected({}); setAccountId(value); }} style={{ width: 280, marginBottom: 16 }} options={accounts.map(a => ({ value: a.id, label: a.nickname }))} />
    {error && <Alert showIcon type="error" title={error} style={{ marginBottom: 16 }} />}
    {success && <Alert showIcon type="success" title={success} style={{ marginBottom: 16 }} />}
    <Card style={{ marginBottom: 24 }}>
      <Space wrap style={{ marginBottom: 20 }}><ApiOutlined /><Typography.Text strong>Multica 统一接入</Typography.Text>
        <Tag color={data?.connected ? 'green' : 'orange'}>{data?.connected ? '已连接' : '连接待检查'}</Tag>
        <Typography.Link href={data?.multica_url || 'https://d.58nft.net:9447'} target="_blank" rel="noreferrer">管理 Multica agent</Typography.Link>
        <Typography.Link href="https://d.58nft.net:9456" target="_blank" rel="noreferrer">管理 Channel 路由</Typography.Link>
      </Space>
      <Steps size="small" items={[{ title: '工作台发起任务', status: 'finish' }, { title: 'Channel 匹配路由', status: 'finish' }, { title: 'Multica agent 执行', status: 'finish' }, { title: '结果返回工作台', status: 'finish' }]} />
      <Typography.Paragraph type="secondary" style={{ marginTop: 20, marginBottom: 0 }}>选择目录中的任意 Agent 并保存，即可用于当前账号的对应任务，无需与账号同名。更换底层模型，请修改 Multica 中对应的 Agent。</Typography.Paragraph>
    </Card>
    {data && !data.connected && <Alert type="warning" showIcon title={data.error || 'Channel 或 Multica 暂不可用'} style={{ marginBottom: 24 }} />}
    <Spin spinning={loading}>
      <Row gutter={[20, 20]}>{purposes.map(purpose => {
        const routes = (data?.routes || []).filter(r => (data?.choices[purpose.id] || []).includes(r.id));
        const route = routes.find(r => r.agent?.id === selected[purpose.id]);
        const agents = data?.agent_choices?.[purpose.id] || [];
        const saved = data?.bindings[purpose.id];
        const selectedAgent = agents.find(a => a.id === selected[purpose.id]);
        const savedAgent = data?.routes.find(r => r.id === saved)?.agent?.id;
        return <Col xs={24} lg={8} key={purpose.id}><Card title={<Space>{purpose.icon}{purpose.title}</Space>} style={{ height: '100%' }}>
          <Typography.Paragraph type="secondary" style={{ minHeight: 66 }}>{purpose.detail}</Typography.Paragraph>
          <Select aria-label={`${purpose.title}路由`} style={{ width: '100%', marginBottom: 20 }} placeholder="选择任意 Agent" value={selected[purpose.id] || undefined}
            options={agents.map(a => ({ value: a.id, label: a.name }))}
            onChange={value => setSelected(current => ({ ...current, [purpose.id]: value }))} />
          {route ? <Descriptions column={1} size="small" items={[
            { key: 'agent', label: '执行助手', children: route.agent?.name },
            { key: 'model', label: '模型', children: route.agent?.model || '由 agent 运行时决定' },
            { key: 'version', label: '路由版本', children: `v${route.version}` },
            { key: 'saved', label: '当前状态', children: <Tag color={savedAgent === route.agent?.id ? 'green' : 'blue'}>{savedAgent === route.agent?.id ? '已启用' : '待保存'}</Tag> },
          ]} /> : selectedAgent ? <Typography.Paragraph>执行 Agent：{selectedAgent.name}。保存后自动绑定当前账号与用途。</Typography.Paragraph> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={saved ? '原绑定已不可用，请重新选择' : '尚未选择 Agent'} />}
          <Button type="primary" block style={{ marginTop: 20 }} loading={saving === purpose.id} disabled={!data?.connected || !selected[purpose.id] || savedAgent === selected[purpose.id]} onClick={() => void save(purpose.id)}>保存用途配置</Button>
        </Card></Col>;
      })}</Row>
    </Spin>
    <Alert style={{ marginTop: 24 }} type="info" showIcon title="私信自动回复按账号开启" description={<>保存私信回复路由后，前往 <Link to="/platforms/xhs/messages">私信监听</Link> 选择 PC 账号。已启用的监听保留原路由；更换后需在监听页重新应用。自动回复只发送模型返回的 reply_text；需人工处理的结果会保留在记录中。</>} />
  </div>;
}
