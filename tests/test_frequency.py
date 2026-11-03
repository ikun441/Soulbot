from dataclasses import replace

from soulbot.app import build_services
from soulbot.core.decision import DecisionEngine
from soulbot.core.events import ChatEvent
from soulbot.services.config_service import GroupPolicy
from soulbot.storage.database import Database


def test_passive_probability_mentions_and_group_overrides(tmp_path):
    s = build_services(tmp_path)
    cfg = s.config.current
    cfg.qq.enabled = True
    cfg.behavior.passive_reply_probability = 0
    private = ChatEvent("34567", "23456", "1", "你好")
    group = replace(private, group_id="56789", directed=True)
    d = s.agent.decision
    assert d.classify(cfg, private) is None
    assert d.classify(cfg, group) == "direct"
    cfg.behavior.always_reply_mentions = False
    assert d.classify(cfg, group) is None
    cfg.behavior.group_policies["56789"] = GroupPolicy(passive_reply_probability=1)
    assert d.classify(cfg, group) == "direct"
    assert d.classify(cfg, private) is None


def test_proactive_probability_cooldown_and_daily_count_survive_restart(tmp_path):
    s = build_services(tmp_path)
    cfg = s.config.current
    cfg.qq.enabled = True
    cfg.behavior.quiet_hours = False
    cfg.behavior.proactive_group_enabled = True
    cfg.behavior.natural_candidate_probability = 0
    cfg.behavior.group_policies["56789"] = GroupPolicy(
        mode="natural",
        allow_proactive=True,
        proactive_probability=1,
        proactive_interval_seconds=10,
        max_proactive_per_day=1,
        reply_cooldown_seconds=20,
    )
    e = ChatEvent("34567", "23456", "1", "聊聊天", group_id="56789")
    now = [1793707200.0]
    d = s.agent.decision
    d.clock = lambda: now[0]
    d.rng = lambda: 0.9
    assert d.classify(cfg, e) == "proactive"
    assert d.claim(cfg, e, "proactive")
    reopened = DecisionEngine(Database(s.db.path), s.agent.conversation, clock=lambda: now[0])
    assert not reopened.claim(cfg, e, "direct")
    now[0] += 21
    assert reopened.claim(cfg, e, "direct")
    assert not reopened.claim(cfg, e, "proactive")
    cfg.behavior.group_policies["56789"].proactive_probability = 0
    assert d.classify(cfg, e) is None
