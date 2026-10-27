import { usePageVisible } from '../../../hooks/use-page-visible';
import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Empty, Form, Input, InputNumber, Select, Space, Switch, Tag, Typography } from 'antd';
import { PageHeader } from '../../../components/layout/app-shell';
import { fetchAccounts, fetchProactive, fetchProactiveAgents, runProactive, saveProactive } from '../../../lib/api';
import type { ProactiveSettings, ProactiveSnapshot, ProactiveSource, AgentChoice } from '../../../lib/api';
import type { PlatformAccount } from '../../../types';
import { formatShanghaiTime } from '../../../lib/time';

const labels: Record<string, string> = {
  waiting_login: '等待账号恢复', waiting_window: '等待每日运行时段', waiting_delivery: '等待私信回执', pending_delivery: '私信排队中', partial: '部分发送成功', sent: '已提交',
  queued: '排队中', running: '正在处理', completed: '本轮完成', skipped: '本轮跳过', failed: '执行失败',
  draft: '历史未发送', ready: '待发送', sending: '正在发送', cancelled: '已停止 · 未发送',
  submitted: '已提交 · 未核验公开可见', uncertain: '结果待核对 · 不重发',
};
const sourceNames = { visitors: '互动回访', recommend: '主动评论探索', outreach: '主动私信探索' };
const sources: ProactiveSource[] = ['visitors', 'recommend', 'outreach'];
const defaults = (source: ProactiveSource): ProactiveSettings => ({ source, instruction: '', enabled: false, interval_minutes: 60,
  browse_limit: 3, comment_limit: 1, keyword: '', comments_per_note: 1, dm_limit: 1, share_note: true, filter_ads: true,
  daily_start: '00:00', daily_end: '23:59', agent_id: '' });
const initial = () => ({ recommend: defaults('recommend'), visitors: defaults('visitors'), outreach: defaults('outreach') });
function errorText(error: unknown) {
  const value = error as { response?: { data?: { detail?: unknown } } };
  return typeof value.response?.data?.detail === 'string' ? value.response.data.detail : '操作未完成，请重试或查看账号状态。';
}

export function ProactivePage() {
  const isPageVisible = usePageVisible();
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [accountId, setAccountId] = useState<number>();
  const [settings, setSettings] = useState(initial);
  const [data, setData] = useState<ProactiveSnapshot>();
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState<Partial<Record<ProactiveSource, boolean>>>({});
  const [agents, setAgents] = useState<AgentChoice[]>([]);
  const [agentLoading, setAgentLoading] = useState(false);
  const [agentError, setAgentError] = useState('');
  const active = useRef(accountId); active.current = accountId;
  async function refreshAgents(selected = accountId, signal?: AbortSignal) {
    if (!selected) return;
    setAgentLoading(true); setAgentError('');
    try {
      const result = await fetchProactiveAgents(selected, signal);
      if (active.current === selected && !signal?.aborted) setAgents(result.agents);
    } catch (error) {
      if (active.current === selected && !signal?.aborted) setAgentError(errorText(error));
    } finally {
      if (active.current === selected && !signal?.aborted) setAgentLoading(false);
    }
  }
  useEffect(() => {
    let alive = true;
    void fetchAccounts('xhs').then(items => {
      if (!alive) return;
      const pc = items.filter(a => a.sub_type === 'pc');
      setAccounts(pc); setAccountId(pc.find(a => a.status === 'active')?.id ?? pc[0]?.id);
    }).catch(error => { if (alive) setError(errorText(error)); });
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    setData(undefined); setDirty({}); setSettings(initial()); setError(''); setNotice(''); setAgents([]); setAgentError('');
    if (!accountId) return;
    const controller = new AbortController(); let pending = false; let first = true;
    void refreshAgents(accountId, controller.signal);
    async function refresh() {
      if (!isPageVisible() || pending) return; pending = true;
      try {
        const result = await fetchProactive(accountId!, controller.signal);
        if (controller.signal.aborted) return;
        setData(result);
        if (first) { setSettings(result.strategies); first = false; }
      } catch (error) { if (!controller.signal.aborted) setError(errorText(error)); }
      finally { pending = false; }
    }
    void refresh(); const timer = setInterval(() => void refresh(), 5000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [accountId]);
  function change(source: ProactiveSource, value: Partial<ProactiveSettings>) {
    setSettings(current => ({ ...current, [source]: { ...current[source], ...value } }));
    setDirty(current => ({ ...current, [source]: true }));
  }
  async function save(source: ProactiveSource, run = false) {
    if (!accountId) return;
    const selected = accountId; setBusy(true); setError(''); setNotice('');
    try {
      const saved = await saveProactive(selected, settings[source]);
      if (run) await runProactive(selected, source);
      if (active.current !== selected) return;
      setSettings(current => ({ ...current, [source]: saved }));
      setDirty(current => ({ ...current, [source]: false }));
      const result = await fetchProactive(selected);
      if (active.current !== selected) return;
      setData(result);
      setNotice(sourceNames[source] + (run ? (source === 'outreach' ? '已开始运行，私信生成后直接发送，发送结果以平台回执为准。' : '已开始运行，评论生成后直接发送。') : '设置已保存。'));
    } catch (error) { if (active.current === selected) setError(errorText(error)); }
    finally { setBusy(false); }
  }
  const nickname = accounts.find(account => account.id === accountId)?.nickname;
  return <>
    <PageHeader eyebrow="XHS" title="主动互动" description="互动回访、评论探索和私信探索分别设置。根据笔记内容生成后直接发送，不合适就跳过。" />
    <Space orientation="vertical" size="middle" style={{ width: '100%', maxWidth: 1000 }}>
      {error && <Alert type="error" showIcon title={error} closable onClose={() => setError('')} />}
      {notice && <Alert type="success" showIcon title={notice} />}
      {agentError && <Alert type="warning" showIcon title={agentError} action={<Button size="small" onClick={() => void refreshAgents()}>刷新 Agent</Button>} />}
      <Card><Form layout="vertical"><Form.Item label="执行账号" style={{ marginBottom: 0 }}><Select style={{ width: '100%' }} value={accountId} disabled={busy} placeholder="选择 PC 账号" onChange={setAccountId} options={accounts.map(a => ({ value: a.id, label: `${a.nickname || a.id} · ${a.status === 'active' ? '已连接' : '待恢复'}` }))} /></Form.Item></Form></Card>
      {sources.map(source => {
        const config = settings[source];
        const running = data?.runs.some(run => run.source === source && ['queued', 'running', 'waiting_login', 'waiting_delivery', 'waiting_window'].includes(run.state));
        const searching = source === 'recommend' && !!config.keyword.trim();
        return <Card key={source} title={sourceNames[source]}>
          <Typography.Paragraph type="secondary">{source === 'visitors' ? '收到点赞或评论 → 进入对方主页 → 评论最新一篇作品。没有作品或读不到就跳过。' : source === 'outreach' ? '按主题搜索笔记 → 阅读内容 → 给作者发送一条相关私信，可同时分享命中的笔记链接。同一作者每轮只联系一次。' : '按主题搜索笔记，每篇分别生成评论；也可以留空主题浏览推荐流。'}</Typography.Paragraph>
          <Form layout="vertical">
            <Form.Item label="执行 Agent"><Space orientation="vertical" style={{ width: '100%' }}>
              <Select aria-label={sourceNames[source] + '执行Agent'} style={{ width: '100%' }} value={config.agent_id || ''} loading={agentLoading} disabled={busy}
                showSearch optionFilterProp="label" onChange={agent_id => change(source, { agent_id })}
                options={[
                  { value: '', label: '沿用账号的内容创作 Agent' },
                  ...agents.map(agent => ({ value: agent.id, label: agent.name })),
                  ...(config.agent_id && !agents.some(agent => agent.id === config.agent_id) ? [{ value: config.agent_id, label: `${config.agent_snapshot?.name || config.agent_id}（需刷新或重新选择）`, disabled: true }] : []),
                ]} />
              <Button size="small" loading={agentLoading} disabled={!accountId} onClick={() => void refreshAgents()}>刷新 Agent 列表</Button>
            </Space><Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>为本策略单独选择 Agent；保存后生效。</Typography.Paragraph></Form.Item>
            <Form.Item label="自动运行"><Switch aria-label={sourceNames[source] + '开关'} checked={config.enabled} disabled={busy} onChange={enabled => change(source, { enabled })} checkedChildren="已开启" unCheckedChildren="已关闭" /></Form.Item>
            <Form.Item label="运行间隔"><Space wrap><Typography.Text>每</Typography.Text><InputNumber aria-label={sourceNames[source] + '间隔分钟'} min={1} max={10080} value={config.interval_minutes} disabled={busy} onChange={value => change(source, { interval_minutes: value ?? 60 })} /><Typography.Text>分钟运行一次</Typography.Text></Space></Form.Item>
            <Form.Item label="每天运行时段（北京时间）"><Space wrap>
              <Input type="time" aria-label={sourceNames[source] + '每天开始时间'} style={{ width: 135 }} value={config.daily_start} disabled={busy} onChange={event => change(source, { daily_start: event.target.value || '00:00' })} />
              <Typography.Text>至</Typography.Text>
              <Input type="time" aria-label={sourceNames[source] + '每天结束时间'} style={{ width: 135 }} value={config.daily_end} disabled={busy} onChange={event => change(source, { daily_end: event.target.value || '23:59' })} />
            </Space><Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>默认全天；结束时间早于开始时间时跨午夜。时段外暂停发送，立即运行也遵循此时段。</Typography.Paragraph></Form.Item>
            {source !== 'visitors' && <Form.Item label={source === 'outreach' ? '搜索主题（必填）' : '搜索主题（留空使用推荐流）'}><Input aria-label={sourceNames[source] + '搜索主题'} value={config.keyword} maxLength={100} disabled={busy} onChange={event => change(source, { keyword: event.target.value })} placeholder="例如：周末露营、徒步装备" /></Form.Item>}
            <Form.Item label="自动过滤明显广告"><Switch aria-label={sourceNames[source] + '过滤广告'} checked={config.filter_ads ?? true} disabled={busy} onChange={filter_ads => change(source, { filter_ads })} checkedChildren="已开启" unCheckedChildren="已关闭" />
              <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>提前过滤平台标记广告和明确销售引导，普通体验分享、个人征友不因“私信”二字被过滤。探索会在同页结果中跳过重复和无效笔记，尽量补足数量；每轮筛选原因见最近运行。</Typography.Paragraph>
            </Form.Item>
            {source !== 'visitors' && <Form.Item label="每轮最多搜索 / 浏览笔记"><Space><InputNumber aria-label={sourceNames[source] + '搜索数量'} min={1} max={20} value={config.browse_limit} disabled={busy} onChange={value => change(source, { browse_limit: value ?? 3 })} /><Typography.Text>篇</Typography.Text></Space></Form.Item>}
            {searching ? <Form.Item label="每篇笔记最多发送评论"><Space><InputNumber aria-label="每篇笔记评论数量" min={1} max={10} value={config.comments_per_note} disabled={busy} onChange={value => change(source, { comments_per_note: value ?? 1 })} /><Typography.Text>条；本轮最多 {config.browse_limit * config.comments_per_note} 条，不重复评论内容</Typography.Text></Space></Form.Item> : source !== 'outreach' && <Form.Item label={source === 'visitors' ? '每轮最多回访用户' : '推荐流每轮最多发送评论'}><Space><InputNumber aria-label={sourceNames[source] + '每轮数量'} min={1} max={10} value={config.comment_limit} disabled={busy} onChange={value => change(source, { comment_limit: value ?? 1 })} /><Typography.Text>{source === 'visitors' ? '人，每人最新作品评论一次' : '条'}</Typography.Text></Space></Form.Item>}
            {source === 'outreach' && <>
              <Form.Item label="每轮最多私信作者"><Space><InputNumber aria-label="每轮私信人数" min={1} max={10} value={config.dm_limit} disabled={busy} onChange={value => change(source, { dm_limit: value ?? 1 })} /><Typography.Text>人，每人一条文字私信</Typography.Text></Space></Form.Item>
              <Form.Item label="同时分享命中的笔记"><Switch aria-label="私信同时分享笔记" checked={config.share_note} disabled={busy} onChange={share_note => change(source, { share_note })} checkedChildren="先分享再私信" unCheckedChildren="只发私信" /><Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>开启后先发送笔记链接，平台确认成功后再发送文字私信。</Typography.Paragraph></Form.Item>
            </>}
            <Form.Item label="互动偏好（选填）"><Input.TextArea aria-label={sourceNames[source] + '互动偏好'} value={config.instruction} disabled={busy} maxLength={1000} rows={2} onChange={event => change(source, { instruction: event.target.value })} placeholder="例如：围绕笔记提到的具体问题自然交流。" /></Form.Item>
            <Space wrap><Button type="primary" loading={busy} disabled={!accountId || !data || running || (source === 'outreach' && !config.keyword.trim())} onClick={() => void save(source, true)}>保存并立即运行{sourceNames[source]}</Button><Button disabled={!accountId || !data || busy || (source === 'outreach' && !config.keyword.trim())} onClick={() => void save(source)}>保存{sourceNames[source]}设置</Button>{dirty[source] && <Tag>未保存</Tag>}</Space>
            {data?.strategies[source].enabled && <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>下次运行：{formatShanghaiTime(data.strategies[source].next_at)}（北京时间）</Typography.Paragraph>}
          </Form>
        </Card>;
      })}
      <Card title="互动结果">
        {!data?.items.length && <Empty description="运行后，这里展示评论、私信内容和发送结果" />}
        {data?.items.map(item => <Card key={item.id} size="small" style={{ marginBottom: 12 }}>
          <Space wrap><Tag>{sourceNames[item.source] || '历史互动'}</Tag><Typography.Text>{item.account_nickname || nickname}</Typography.Text><Tag color={item.state === 'submitted' ? 'blue' : item.state === 'uncertain' ? 'orange' : 'default'}>{labels[item.state] || item.state}</Tag></Space>
          {item.agent_snapshot?.name && <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>执行 Agent：{item.agent_snapshot.name}</Typography.Paragraph>}
          <Typography.Paragraph style={{ marginTop: 12 }}><a href={item.note_url} target="_blank" rel="noreferrer">{item.title || '查看原帖'}</a> · {item.author_name}</Typography.Paragraph>
          {item.interaction === 'dm' && <Typography.Paragraph type="secondary">{item.share_note ? <a href={item.share_note_url} target="_blank" rel="noreferrer">同时分享：{item.title || '命中的笔记'}</a> : '仅发送文字私信'}</Typography.Paragraph>}
          <Typography.Text type="secondary">{item.interaction === 'dm' ? '私信内容' : '评论内容' + (item.sequence ? ` · 本篇第 ${item.sequence} 条` : '')}</Typography.Text>
          <Typography.Paragraph copyable style={{ fontSize: 17, whiteSpace: 'pre-wrap', marginTop: 8 }}>{item.text}</Typography.Paragraph>
          <Typography.Paragraph type="secondary">{item.sent_at ? '发送时间：' + formatShanghaiTime(item.sent_at) : item.attempted_at ? '尝试发送时间：' + formatShanghaiTime(item.attempted_at) : '生成时间：' + formatShanghaiTime(item.created_at)}（北京时间）</Typography.Paragraph>
          <details><summary>查看依据和回执</summary>
            <Typography.Paragraph type="secondary">{item.reason}</Typography.Paragraph>
            {item.references.map((text, index) => <Typography.Paragraph key={index} type="secondary">参考评论：{text}</Typography.Paragraph>)}
            {item.reference_status && <Typography.Paragraph type="secondary">{item.reference_status}</Typography.Paragraph>}
            {item.detail && <Typography.Paragraph type="secondary">{item.detail}</Typography.Paragraph>}
            {item.messages?.map((message, index) => <Typography.Paragraph key={index} type="secondary">{message.kind === 'note_link' ? '笔记分享' : '文字私信'}：{labels[message.state] || message.state}{message.detail ? ' · ' + message.detail : ''}{message.external_id ? ' · ID：' + message.external_id : ''}</Typography.Paragraph>)}
            {item.external_id && <Typography.Text type="secondary">{item.interaction === 'dm' ? '消息' : '评论'} ID：{item.external_id}</Typography.Text>}
          </details>
        </Card>)}
      </Card>
      <Card title="最近运行">
        {!data?.runs.length && <Empty description="还没有运行记录" />}
        {data?.runs.map(run => <div key={run.id} style={{ marginBottom: 16 }}>
          <Typography.Paragraph><Tag>{sourceNames[run.source] || '历史互动'}</Tag><Tag color={run.state === 'failed' ? 'red' : 'default'}>{labels[run.state] || run.state}</Tag>{formatShanghaiTime(run.created_at)}　{run.detail}</Typography.Paragraph>
          {run.diagnostics?.counts && <>
            <Typography.Paragraph type="secondary">扫描候选 {run.diagnostics.scanned ?? 0} 条；交给 Agent {run.diagnostics.prepared ?? 0} 篇；过滤广告 {run.diagnostics.counts.ad ?? 0} 篇；跳过重复或冷却 {run.diagnostics.counts.duplicate ?? 0} 篇；Agent 跳过 {run.diagnostics.counts.ai_skipped ?? 0} 次。</Typography.Paragraph>
            <details><summary>查看逐篇筛选与跳过原因</summary>
              {run.diagnostics.records?.map((note, index) => <Typography.Paragraph key={index} type="secondary">{note.title || note.note_id || '搜索推荐项'}{note.author_name ? ` · ${note.author_name}` : ''}：{note.reason}</Typography.Paragraph>)}
              {run.diagnostics.read_budget_reached && <Typography.Paragraph type="secondary">已达到本轮读取上限，余下结果本轮未检查。</Typography.Paragraph>}
            </details>
          </>}
        </div>)}
      </Card>
    </Space>
  </>;
}
