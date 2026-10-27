"""Install focused, account-bound reply agents without changing creation agents."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'channel'))
from channel_service.multica import Multica

PRIVATE = ROOT / 'xhs/.local/fast-reply'


def instructions(persona, account):
    sys.path.insert(0, str(ROOT / 'agent-tools/social-content-kit/scripts'))
    from compose import persona_prompt, skill_body
    return ('你只为绑定的小红书账号起草私信，平台执行账号仅允许：' + account + '。'
        '完整冻结快照已直接提供，context.current_messages 是本轮来话，context.messages 是此前历史；'
        '只依据这些消息和 context.memories；'
        '禁止任何资料读取工具调用，不读取 issue、评论、其他账号、旧任务、本地记忆库或凭据。'
        '不写文件，不发评论，不执行发送或其他平台操作。真实图片已在本轮附件中提供时可查看该附件，'
        '不下载外部候选素材，不为纯文字回复调用工具。'
        '下方账号档案和对话能力已完整加载，不需要再用工具加载技能或资料。'
        '当前任务仅需最终 JSON，直接输出契约对象，不输出开场白、计划或分析过程。'
        '先核对账号绑定与当前说话方向。'
        '不把已确认自动生成的历史作为本人风格样本。短句不自动等于结束。'
        '以 current_messages 和 trigger_message_ids 对应的最新来话判断：当前明确停止才 no_reply；'
        '对方在停止之后主动说继续、提出新问题或重开话题，应回应新来话；旧分析不能覆盖新意图。'
        '只在确实需要理解或选用图片时查看当前附件，未查看不得发送或描述图片。'
        '分析字段简短，尽量一到两条自然回复，delay_seconds=0；不强行追问或延长已结束对话。'
        '\n\n账号档案：\n' + persona_prompt(persona) + '\n\n当前已加载 social-interaction-dialogue：\n'
        + skill_body('social-interaction-dialogue') + '\n\n'
        '以当前工作台 JSON 契约为交付格式，不返回 ContentPackage 或额外报告。')


def install(model='gpt-6-luna'):
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    remote = Multica(ROOT / 'channel/.local/multica.json')
    existing = {a['name']: a for a in remote.agents()}
    agents = {}
    for persona, account, name in [('xhs-leo', 'user:1:account:5cd2ac18000000001200cf4b', '小红书-leo · 私信'),
                                  ('xiaotao', 'user:1:account:60d73b36000000002002dc93', '小红书-小桃 · 私信')]:
        payload = dict(name=name, description='账号隔离的即时私信回复；创作和研究沿用原业务 Agent。',
            instructions=instructions(persona, account), model=model, thinking_level='low',
            runtime_id='ed2437c9-fa66-4d96-8f0b-15069f1cd9f5', max_concurrent_tasks=2,
            permission_mode='private', skill_ids=[], mcp_config={}, service_tier='priority')
        old = existing.get(name)
        agent = remote.request('PUT' if old else 'POST', '/api/agents' + ('/' + old['id'] if old else ''), payload)
        agents[persona] = {k:agent[k] for k in ('id', 'name', 'model', 'runtime_id')}
    path = PRIVATE / 'agents.json'
    path.write_text(json.dumps(agents, ensure_ascii=False, indent=2)); path.chmod(0o600)
    print(json.dumps(agents, ensure_ascii=False))
    return agents


if __name__ == '__main__':
    install()
