"""Pure emergency-scope policy evaluation. No hardware, network, or automatic reset."""
from dataclasses import asdict, dataclass

POLICY_VERSION = '0.5'
FACT_EVENTS = {'求救', '无法起身', '跌倒', '受伤', '请求帮助', '疼痛', '明确安全',
               '呼吸困难', '站立不稳', '意识异常'}
RISK_EVENTS = {'无法起身', '受伤', '疼痛', '呼吸困难', '站立不稳', '意识异常'}


@dataclass(frozen=True)
class PolicyDecision:
    route: str
    red_alert_before: bool
    proposed_red_alert_latched: bool
    new_red_trigger: bool
    confirmation_state: str
    follow_up_required: bool
    reason: str
    policy_version: str = POLICY_VERSION
    evaluation_only: bool = True

    def to_dict(self):
        return asdict(self)


def decide_response(events, understood, awaiting=False, red_alert_before=False):
    """Consume facts, not transcript keywords. Proposed state only, never IO.

    Absence of new trigger never clears a prior red latch. Caregiver resolution
    is deliberately outside this speech policy, requiring a separate control.
    """
    if type(understood) is not bool or type(awaiting) is not bool or type(red_alert_before) is not bool:
        raise ValueError('understood/context/latch must be booleans')
    if not isinstance(events, (list, tuple, set, frozenset)) or any(not isinstance(e, str) or e not in FACT_EVENTS for e in events):
        raise ValueError('unknown event or invalid event collection')
    e = set(events)
    trigger = bool(e & {'求救', '跌倒'})
    if '求救' in e:
        route, reason = 'emergency_help', '明确紧急呼救，建议立即红色锁存告警。'
    elif '跌倒' in e:
        route, reason = 'fall_or_injury', '明确跌倒；已起身或自称没事不抵消告警。'
    elif '受伤' in e:
        route, reason = 'fall_or_injury', '明确受伤，进入关注核查；单独受伤的细分告警尚未确定。'
    elif e & RISK_EVENTS:
        route, reason = 'uncertain', '明确身体风险线索，进行一次安全核查；不推断跌倒、病名或紧急等级。'
    elif awaiting and '请求帮助' in e:
        route, reason = 'needs_assistance', '安全询问中收到协助请求；仍需核查，不视为安全确认。'
    elif not understood:
        route, reason = 'uncertain', '本句未可靠理解；无既有安全询问时仅保留诊断，不新建核查，也不证明安全。'
    elif awaiting and '明确安全' in e:
        route, reason = 'safety_response', '得到明确安全回答；不解除已锁存红色告警。'
    elif awaiting:
        route, reason = 'continue_safety_confirmation', '尚未得到明确安全回答，继续确认。'
    else:
        route, reason = 'normal', '日常请求或闲聊不作处理；本句未触发相关处理，不代表整个现场安全。'
    confirmation = ('answered_safe' if route == 'safety_response' else
                    ('pending' if awaiting else 'not_pending'))
    return PolicyDecision(route, red_alert_before, red_alert_before or trigger,
        trigger and not red_alert_before, confirmation,
        trigger or bool(e & RISK_EVENTS) or (awaiting and route != 'safety_response') or red_alert_before,
        reason)
