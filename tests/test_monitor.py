import datetime as dt
import tempfile
import unittest

import sub2api_monitor as m


class PredicateTests(unittest.TestCase):
    def test_status_ranges(self):
        ranges = m.parse_allowed_status("400,429,500-599")
        self.assertTrue(m.status_allowed(400, ranges))
        self.assertTrue(m.status_allowed(429, ranges))
        self.assertTrue(m.status_allowed(503, ranges))
        self.assertFalse(m.status_allowed(401, ranges))

    def test_default_upstream_error_statuses_include_bad_request(self):
        mon = m.Monitor(m.Config(), dry_run=True)
        self.assertTrue(m.status_allowed(400, mon.allowed_status))

    def test_error_filter_excludes_auth_and_network(self):
        cfg = m.Config()
        mon = m.Monitor(cfg, dry_run=True)
        base = {
            "error_owner": "provider",
            "error_source": "upstream_http",
            "network_error_type": "",
            "is_business_limited": False,
            "upstream_status_code": 503,
            "message": "Recovered upstream error 503",
        }
        self.assertTrue(mon.is_actionable_upstream_error(dict(base)))
        row = dict(base, message="Encountered invalidated oauth token for user")
        self.assertFalse(mon.is_actionable_upstream_error(row))
        row = dict(base, network_error_type="timeout")
        self.assertFalse(mon.is_actionable_upstream_error(row))
        row = dict(base, error_owner="client")
        self.assertFalse(mon.is_actionable_upstream_error(row))


    def test_proxy_error_detects_network_and_proxy_failures(self):
        cfg = m.Config()
        mon = m.Monitor(cfg, dry_run=True)
        base = {
            "id": 1,
            "error_owner": "provider",
            "error_source": "upstream_http",
            "network_error_type": "",
            "is_business_limited": False,
            "status_code": None,
            "upstream_status_code": None,
            "message": "proxy CONNECT failed: ECONNREFUSED",
        }
        self.assertTrue(mon.is_actionable_proxy_error(dict(base)))
        row = dict(base, message="plain provider 503", upstream_status_code=503)
        self.assertFalse(mon.is_actionable_proxy_error(row))
        row = dict(base, network_error_type="timeout", message="request timed out")
        self.assertTrue(mon.is_actionable_proxy_error(row))
        row = dict(base, error_owner="client", network_error_type="timeout", message="client timeout")
        self.assertFalse(mon.is_actionable_proxy_error(row))
        row = dict(base, message="invalid api key timeout")
        self.assertFalse(mon.is_actionable_proxy_error(row))



    def test_upstream_error_message_includes_account_and_proxy_context(self):
        cfg = m.Config()
        grouped = {
            "upstream:x": {
                "count": 1,
                "sample": {
                    "id": 5766,
                    "platform": "openai",
                    "model": "gpt-5.5",
                    "upstream_status_code": 429,
                    "message": "The usage limit has been reached",
                    "account_id": 108,
                    "account_name": "New Account",
                    "account_plan": "plus",
                    "account_status": "active",
                    "proxy_id": 7,
                    "proxy_name": "LA Proxy A",
                    "proxy_protocol": "socks5",
                    "proxy_status": "active",
                    "request_id": "req_abcdefghijklmnopqrstuvwxyz",
                },
                "rows": [
                    {
                        "id": 5766,
                        "account_id": 108,
                        "account_name": "New Account",
                        "account_plan": "plus",
                        "account_status": "active",
                        "proxy_id": 7,
                        "proxy_name": "LA Proxy A",
                        "proxy_protocol": "socks5",
                        "proxy_status": "active",
                        "request_id": "req_abcdefghijklmnopqrstuvwxyz",
                    }
                ],
            }
        }
        message = m.build_error_message(grouped, 0, cfg)
        self.assertIn("openai/gpt-5.5", message)
        self.assertIn("代理：LA Proxy A", message)
        self.assertIn("账号：#108 Ne***nt (plus, active)", message)
        self.assertIn("原因：The usage limit has been reached", message)
        self.assertNotIn("req req_", message)
        self.assertNotIn("ids 5766", message)

    def test_proxy_display_label_does_not_need_proxy_url(self):
        self.assertEqual(
            m.proxy_display_label({"proxy_id": 7, "proxy_name": "LA Proxy A", "proxy_protocol": "socks5", "proxy_status": "active"}),
            "LA Proxy A (socks5, active)",
        )
        self.assertEqual(m.proxy_display_label({"proxy_id": 7}), "#7")
        self.assertEqual(m.proxy_display_label({}), "")

    def test_proxy_error_message_groups_account_ids(self):
        cfg = m.Config()
        grouped = {
            "proxy:x": {
                "count": 2,
                "sample": {
                    "id": 10,
                    "platform": "openai",
                    "model": "gpt-test",
                    "network_error_type": "proxy_connect",
                    "message": "proxy CONNECT failed",
                    "account_id": 101,
                    "proxy_id": 7,
                    "proxy_name": "LA Proxy A",
                    "proxy_protocol": "socks5",
                    "proxy_status": "active",
                },
                "rows": [
                    {"id": 10, "account_id": 101},
                    {"id": 11, "account_id": 102},
                ],
            }
        }
        message = m.build_proxy_error_message(grouped, 0, cfg)
        self.assertIn("出口/网络错误", message)
        self.assertIn("openai/gpt-test", message)
        self.assertIn("proxy_connect", message)
        self.assertIn("代理：LA Proxy A (socks5, active)", message)
        self.assertIn("账号：#101", message)
        self.assertIn("#102", message)
        self.assertIn("原因：proxy CONNECT failed", message)
        self.assertNotIn("ids 10,11", message)

    def test_account_digest_ignores_usage_percent(self):
        row = {
            "id": 1,
            "platform": "openai",
            "type": "oauth",
            "plan": "plus",
            "status": "active",
            "normal": True,
            "schedulable": True,
            "rate_limited": False,
            "codex_5h_used_percent": 10,
        }
        a = m.account_digest(row)
        row["codex_5h_used_percent"] = 99
        b = m.account_digest(row)
        self.assertEqual(a, b)

    def test_account_change_message_shows_before_after(self):
        cfg = m.Config()
        current = {
            "id": 104,
            "platform": "openai",
            "type": "oauth",
            "plan": "plus",
            "status": "active",
            "normal": True,
            "schedulable": True,
            "rate_limited": False,
            "overloaded": False,
            "temp_unschedulable": False,
            "expired": False,
            "email": "someone@example.com",
            "codex_5h_used_percent": 100.0,
            "codex_7d_used_percent": 46.0,
        }
        previous = dict(current)
        previous.update({
            "normal": False,
            "rate_limited": True,
            "rate_limit_reset_at": "2026-06-05 12:49:00+00",
        })
        changed = dict(current)
        changed["_previous"] = m.account_digest(previous)

        message = m.build_account_message([current], [changed], [], [], cfg, title="test")

        self.assertIn("本次变化", message)
        self.assertIn("状态：限流至 06-05 12:49 → 正常", message)
        self.assertIn("当前用量：<code>5h 100.0%</code>（可能未刷新） · <code>7d 46.0%</code>", message)
        self.assertNotIn("当前需要关注", message)

    def test_normal_full_quota_marks_all_windows_as_waiting_refresh(self):
        row = {
            "status": "active",
            "normal": True,
            "schedulable": True,
            "rate_limited": False,
            "codex_5h_used_percent": 100,
            "codex_7d_used_percent": 100,
        }

        self.assertEqual(
            m.account_quota_summary(row),
            "<code>5h 100%</code>（可能未刷新） · <code>7d 100%</code>（可能未刷新）",
        )
        self.assertEqual(
            m.plain_account_quota_summary(row),
            "5h 100%（可能未刷新） · 7d 100%（可能未刷新）",
        )

    def test_active_limited_full_quota_does_not_mark_waiting_refresh(self):
        row = {
            "status": "active",
            "normal": False,
            "schedulable": False,
            "rate_limited": True,
            "codex_5h_used_percent": 100,
            "codex_7d_used_percent": 100,
        }

        self.assertEqual(
            m.account_quota_summary(row),
            "<code>5h 100%</code> · <code>7d 100%</code>",
        )
        self.assertEqual(
            m.plain_account_quota_summary(row),
            "5h 100% · 7d 100%",
        )

    def test_user_recharge_message_redacts_and_summarizes(self):
        cfg = m.Config()
        message = m.build_user_recharge_message(
            [
                {
                    "id": 9,
                    "email": "28abcd@qq.com",
                    "username": "member",
                    "role": "user",
                    "status": "active",
                    "balance": "32.90506360",
                    "total_recharged": "1060.00000000",
                    "updated_at": "2026-06-01 12:10:14+08",
                    "_recharge_delta": "50.00000000",
                }
            ],
            cfg,
        )

        self.assertIn("用户充值", message)
        self.assertIn("新增 1 笔 / 合计 +50", message)
        self.assertIn("#9", message)
        self.assertIn("28***@qq***", message)
        self.assertIn("(user, active)", message)
        self.assertIn("充值：+50 · 累计：1,060 · 余额：32.905064", message)
        self.assertNotIn("28abcd@qq.com", message)

    def test_user_recharge_check_baselines_then_alerts_increases(self):
        class FakeMonitor(m.Monitor):
            def __init__(self, cfg, snapshots):
                self.snapshots = list(snapshots)
                super().__init__(cfg, dry_run=True)

            def current_user_recharge_rows(self):
                return self.snapshots.pop(0)

            def baseline_recharge_event_cursors(self):
                self.state["recharge_events_initialized"] = True

            def fetch_new_recharge_events(self):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            cfg = m.Config(state_file=f"{tmp}/state.json")
            mon = FakeMonitor(
                cfg,
                [
                    [
                        {
                            "id": 6,
                            "email": "downstream@example.com",
                            "username": "",
                            "role": "user",
                            "status": "active",
                            "balance": "10.00000000",
                            "total_recharged": "100.00000000",
                        }
                    ],
                    [
                        {
                            "id": 6,
                            "email": "downstream@example.com",
                            "username": "",
                            "role": "user",
                            "status": "active",
                            "balance": "35.00000000",
                            "total_recharged": "125.00000000",
                        }
                    ],
                ],
            )

            self.assertIsNone(mon.check_user_recharges())
            message = mon.check_user_recharges()

        self.assertIn("用户充值", message)
        self.assertIn("充值：+25", message)
        self.assertIn("累计：125", message)

    def test_recharge_event_message_includes_balance_and_subscription_records(self):
        cfg = m.Config()
        message = m.build_recharge_event_message(
            [
                {
                    "source": "redeem_code",
                    "event_key": "redeem:10",
                    "record_id": 10,
                    "event_at": "2026-06-01 12:10:14+08",
                    "user_id": 9,
                    "email": "28abcd@qq.com",
                    "username": "member",
                    "role": "user",
                    "status": "active",
                    "balance": "32.90506360",
                    "total_recharged": "1060.00000000",
                    "type": "balance",
                    "value": "50.00000000",
                    "code": "SECRET-CODE-SHOULD-NOT-LEAK",
                },
                {
                    "source": "redeem_code",
                    "event_key": "redeem:11",
                    "record_id": 11,
                    "event_at": "2026-06-01 12:11:14+08",
                    "user_id": 9,
                    "email": "28abcd@qq.com",
                    "username": "member",
                    "role": "user",
                    "status": "active",
                    "type": "subscription",
                    "value": "0",
                    "group_id": 3,
                    "group_name": "Claude Max",
                    "validity_days": 30,
                },
            ],
            cfg,
        )

        self.assertIn("用户充值/兑换", message)
        self.assertIn("余额合计 +50", message)
        self.assertIn("余额充值/兑换：+50", message)
        self.assertIn("订阅兑换/续期：Claude Max · 30 天", message)
        self.assertIn("兑换记录#10", message)
        self.assertIn("兑换记录#11", message)
        self.assertNotIn("SECRET-CODE-SHOULD-NOT-LEAK", message)

    def test_recharge_event_check_alerts_redeem_records_and_dedupes_total_fallback(self):
        class FakeMonitor(m.Monitor):
            def __init__(self, cfg, user_snapshots, event_snapshots):
                self.user_snapshots = list(user_snapshots)
                self.event_snapshots = list(event_snapshots)
                super().__init__(cfg, dry_run=True)

            def current_user_recharge_rows(self):
                return self.user_snapshots.pop(0)

            def baseline_recharge_event_cursors(self):
                self.state["recharge_events_initialized"] = True
                self.state["last_redeem_code_event_at"] = "2026-06-01 00:00:00+08"
                self.state["last_redeem_code_event_id"] = 1

            def fetch_new_recharge_events(self):
                return self.event_snapshots.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = m.Config(state_file=f"{tmp}/state.json")
            mon = FakeMonitor(
                cfg,
                [
                    [
                        {
                            "id": 6,
                            "email": "downstream@example.com",
                            "username": "",
                            "role": "user",
                            "status": "active",
                            "balance": "10.00000000",
                            "total_recharged": "100.00000000",
                        }
                    ],
                    [
                        {
                            "id": 6,
                            "email": "downstream@example.com",
                            "username": "",
                            "role": "user",
                            "status": "active",
                            "balance": "35.00000000",
                            "total_recharged": "125.00000000",
                        }
                    ],
                ],
                [
                    [
                        {
                            "source": "redeem_code",
                            "event_key": "redeem:88",
                            "record_id": 88,
                            "event_at": "2026-06-01 12:10:14+08",
                            "user_id": 6,
                            "email": "downstream@example.com",
                            "username": "",
                            "role": "user",
                            "status": "active",
                            "balance": "35.00000000",
                            "total_recharged": "125.00000000",
                            "type": "balance",
                            "value": "25.00000000",
                        }
                    ]
                ],
            )

            self.assertIsNone(mon.check_user_recharges())
            message = mon.check_user_recharges()

        self.assertIn("用户充值/兑换", message)
        self.assertIn("兑换记录#88", message)
        self.assertIn("余额充值/兑换：+25", message)
        self.assertNotIn("累计充值差额兜底", message)

    def test_recharge_event_check_alerts_subscription_without_total_recharged_increase(self):
        class FakeMonitor(m.Monitor):
            def __init__(self, cfg, user_snapshots, event_snapshots):
                self.user_snapshots = list(user_snapshots)
                self.event_snapshots = list(event_snapshots)
                super().__init__(cfg, dry_run=True)

            def current_user_recharge_rows(self):
                return self.user_snapshots.pop(0)

            def baseline_recharge_event_cursors(self):
                self.state["recharge_events_initialized"] = True

            def fetch_new_recharge_events(self):
                return self.event_snapshots.pop(0)

        user_row = {
            "id": 7,
            "email": "subscriber@example.com",
            "username": "",
            "role": "user",
            "status": "active",
            "balance": "0",
            "total_recharged": "0",
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = m.Config(state_file=f"{tmp}/state.json")
            mon = FakeMonitor(
                cfg,
                [[dict(user_row)], [dict(user_row)]],
                [
                    [
                        {
                            "source": "redeem_code",
                            "event_key": "redeem:99",
                            "record_id": 99,
                            "event_at": "2026-06-01 12:10:14+08",
                            "user_id": 7,
                            "email": "subscriber@example.com",
                            "role": "user",
                            "status": "active",
                            "type": "subscription",
                            "value": "0",
                            "group_name": "Claude Max",
                            "validity_days": 30,
                        }
                    ]
                ],
            )

            self.assertIsNone(mon.check_user_recharges())
            message = mon.check_user_recharges()

        self.assertIn("新增 1 条兑换/订阅事件", message)
        self.assertIn("订阅兑换/续期：Claude Max · 30 天", message)

    def test_content_moderation_message_redacts_and_summarizes_triggers(self):
        cfg = m.Config()
        message = m.build_content_moderation_message(
            [
                {
                    "id": 101,
                    "created_at": "2026-06-01 12:10:14+08",
                    "user_id": 9,
                    "user_email": "member@example.com",
                    "api_key_id": 2,
                    "api_key_name": "Claude Key",
                    "group_name": "Claude Max",
                    "provider": "anthropic",
                    "model": "claude-sonnet-4",
                    "mode": "pre_block",
                    "action": "keyword_block",
                    "flagged": True,
                    "highest_category": "keyword",
                    "highest_score": "1.0",
                    "input_excerpt": "blocked prompt with user secret-token",
                    "violation_count": 3,
                    "auto_banned": False,
                    "email_sent": True,
                },
                {
                    "id": 102,
                    "created_at": "2026-06-01 12:11:14+08",
                    "user_id": 12,
                    "user_email": "abuse@example.com",
                    "api_key_name": "OpenAI Key",
                    "group_name": "GPT Plus",
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "mode": "pre_block",
                    "action": "block",
                    "flagged": True,
                    "highest_category": "violence",
                    "highest_score": "0.982",
                    "input_excerpt": "harmful prompt",
                    "violation_count": 10,
                    "auto_banned": True,
                    "email_sent": False,
                    "request_id": "req_should_not_leak",
                },
            ],
            cfg,
        )

        self.assertIn("风控触发", message)
        self.assertIn("新增 2 条 / 命中 2 / 拦截 2 / 自动封禁 1", message)
        self.assertIn("用户 #9 me***@ex***", message)
        self.assertIn("API Key：Claude Key", message)
        self.assertIn("动作：关键词拦截", message)
        self.assertIn("类别：keyword · 分数：1", message)
        self.assertIn("次数：3 / 自动封禁：否 / 邮件：已发", message)
        self.assertIn("用户 #12 ab***@ex***", message)
        self.assertIn("自动封禁：是", message)
        self.assertIn("摘要：blocked prompt with user secret-token", message)
        self.assertNotIn("member@example.com", message)
        self.assertNotIn("abuse@example.com", message)
        self.assertNotIn("req_should_not_leak", message)

    def test_content_moderation_check_baselines_then_alerts_only_triggers(self):
        class FakeMonitor(m.Monitor):
            def __init__(self, cfg, snapshots):
                self.snapshots = list(snapshots)
                super().__init__(cfg, dry_run=True)

            def content_moderation_logs_available(self):
                return True

            def max_content_moderation_log_id(self):
                rows = self.snapshots.pop(0)
                return max((int(row.get("id") or 0) for row in rows), default=0)

            def fetch_new_content_moderation_rows(self):
                return self.snapshots.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = m.Config(state_file=f"{tmp}/state.json")
            mon = FakeMonitor(
                cfg,
                [
                    [
                        {"id": 1, "action": "keyword_block", "flagged": True, "user_email": "old@example.com"},
                    ],
                    [
                        {"id": 2, "action": "allow", "flagged": False, "user_email": "clean@example.com"},
                        {
                            "id": 3,
                            "action": "block",
                            "flagged": True,
                            "user_email": "member@example.com",
                            "highest_category": "violence",
                            "highest_score": "0.982",
                        },
                    ],
                ],
            )

            self.assertIsNone(mon.check_content_moderation_logs())
            message = mon.check_content_moderation_logs()

        self.assertIn("风控触发", message)
        self.assertIn("新增 1 条", message)
        self.assertIn("用户 me***@ex***", message)
        self.assertNotIn("clean@example.com", message)
        self.assertEqual(mon.state["last_content_moderation_log_id"], 3)

    def test_settings_change_message_is_human_readable_and_secret_safe(self):
        cfg = m.Config()
        before = m.settings_audit_snapshot(
            m.settings_audit_spec_by_table()["accounts"],
            {
                "id": 108,
                "name": "Primary OpenAI",
                "platform": "openai",
                "type": "oauth",
                "credentials": {"api_key": "sk-old-secret"},
                "concurrency": 3,
                "priority": 50,
            },
        )
        after = m.settings_audit_snapshot(
            m.settings_audit_spec_by_table()["accounts"],
            {
                "id": 108,
                "name": "Primary OpenAI",
                "platform": "openai",
                "type": "oauth",
                "credentials": {"api_key": "sk-new-secret"},
                "concurrency": 5,
                "priority": 50,
            },
        )

        message = m.build_settings_change_message(
            [
                {
                    "table": "accounts",
                    "category": "账号管理",
                    "record_label": after["label"],
                    "operation": "changed",
                    "before": before,
                    "after": after,
                    "changed_fields": m.audit_changed_fields(before, after),
                }
            ],
            cfg,
        )

        self.assertIn("设置变更", message)
        self.assertIn("账号管理", message)
        self.assertIn("账号 #108 Primary OpenAI", message)
        self.assertIn("并发：3 → 5", message)
        self.assertIn("凭证：已更新", message)
        self.assertNotIn("sk-old-secret", message)
        self.assertNotIn("sk-new-secret", message)

    def test_account_settings_audit_ignores_runtime_oauth_token_rotation(self):
        spec = m.settings_audit_spec_by_table()["accounts"]
        base = {
            "id": 108,
            "name": "Primary OpenAI",
            "credentials": {
                "access_token": "runtime-old",
                "expires_at": "2026-06-01T00:00:00Z",
                "refresh_token": "stable-refresh",
            },
            "extra": {
                "codex_5h_used_percent": 10,
                "codex_5h_reset_at": "2026-06-01T00:00:00Z",
                "codex_usage_updated_at": "2026-06-01T00:00:00Z",
                "codex_cli_only_allowed_clients": ["claude_code"],
            },
        }
        rotated = dict(base)
        rotated["credentials"] = {
            "access_token": "runtime-new",
            "expires_at": "2026-06-01T01:00:00Z",
            "refresh_token": "stable-refresh",
        }
        rotated["extra"] = {
            "codex_5h_used_percent": 95,
            "codex_5h_reset_at": "2026-06-01T01:00:00Z",
            "codex_usage_updated_at": "2026-06-01T01:00:00Z",
            "codex_cli_only_allowed_clients": ["claude_code"],
        }
        changed_refresh = dict(rotated)
        changed_refresh["credentials"] = dict(rotated["credentials"], refresh_token="changed-refresh")

        self.assertEqual(
            m.settings_audit_snapshot(spec, base)["fingerprint"],
            m.settings_audit_snapshot(spec, rotated)["fingerprint"],
        )
        self.assertNotEqual(
            m.settings_audit_snapshot(spec, rotated)["fingerprint"],
            m.settings_audit_snapshot(spec, changed_refresh)["fingerprint"],
        )

    def test_settings_change_check_baselines_then_alerts_multiple_admin_areas(self):
        class FakeMonitor(m.Monitor):
            def __init__(self, cfg, snapshots):
                self.snapshots = list(snapshots)
                super().__init__(cfg, dry_run=True)

            def current_settings_audit_rows(self):
                return self.snapshots.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = m.Config(state_file=f"{tmp}/state.json")
            mon = FakeMonitor(
                cfg,
                [
                    {
                        "accounts": [
                            {"id": 108, "name": "Primary OpenAI", "platform": "openai", "type": "oauth", "concurrency": 3}
                        ],
                        "users": [
                            {"id": 9, "email": "member@example.com", "username": "member", "role": "user", "status": "active"}
                        ],
                        "settings": [{"id": "payment_config", "key": "payment_config", "value": "{\"enabled\":false}"}],
                    },
                    {
                        "accounts": [
                            {"id": 108, "name": "Primary OpenAI", "platform": "openai", "type": "oauth", "concurrency": 5}
                        ],
                        "users": [
                            {"id": 9, "email": "member@example.com", "username": "member", "role": "admin", "status": "active"}
                        ],
                        "settings": [{"id": "payment_config", "key": "payment_config", "value": "{\"enabled\":true}"}],
                    },
                ],
            )

            self.assertIsNone(mon.check_settings_changes())
            message = mon.check_settings_changes()

        self.assertIn("设置变更", message)
        self.assertIn("账号管理", message)
        self.assertIn("账号 #108 Primary OpenAI", message)
        self.assertIn("并发：3 → 5", message)
        self.assertIn("用户管理", message)
        self.assertIn("用户 #9 me***@ex***", message)
        self.assertIn("角色：user → admin", message)
        self.assertIn("系统设置", message)
        self.assertIn("系统设置 payment_config", message)
        self.assertNotIn("member@example.com", message)

    def test_telegram_bot_commands_are_valid_for_api(self):
        for command in m.TELEGRAM_BOT_COMMANDS:
            self.assertRegex(command["command"], r"^[a-z0-9_]{1,32}$")
            self.assertGreaterEqual(len(command["description"]), 1)
            self.assertLessEqual(len(command["description"]), 256)
        self.assertIn({"command": "update", "description": "检查程序更新"}, m.TELEGRAM_BOT_COMMANDS)

    def test_set_telegram_bot_commands_payload(self):
        calls = []
        original = m.telegram_api_request

        def fake_request(cfg, method, payload, timeout_seconds=20):
            calls.append((method, payload, timeout_seconds))
            return {"ok": True, "result": True}

        try:
            m.telegram_api_request = fake_request
            m.set_telegram_bot_commands(m.Config(telegram_bot_token="123:token"))
        finally:
            m.telegram_api_request = original

        self.assertEqual(len(calls), 1)
        method, payload, timeout_seconds = calls[0]
        self.assertEqual(method, "setMyCommands")
        commands = m.json.loads(payload["commands"])
        self.assertEqual(commands, m.TELEGRAM_BOT_COMMANDS)
        self.assertEqual(timeout_seconds, 20)

    def test_send_telegram_can_include_inline_keyboard(self):
        calls = []
        original = m.telegram_api_request
        reply_markup = {
            "inline_keyboard": [
                [{"text": "🔄 立即更新", "callback_data": "sub2api:update:run"}],
            ]
        }

        def fake_request(cfg, method, payload, timeout_seconds=20):
            calls.append((method, payload, timeout_seconds))
            return {"ok": True, "result": True}

        try:
            m.telegram_api_request = fake_request
            m.send_telegram(
                m.Config(telegram_bot_token="123:token"),
                "发现新版本",
                chat_id="123",
                reply_markup=reply_markup,
            )
        finally:
            m.telegram_api_request = original

        self.assertEqual(len(calls), 1)
        method, payload, _timeout_seconds = calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(m.json.loads(payload["reply_markup"]), reply_markup)

    def test_update_button_label_has_update_icon(self):
        reply_markup = m.build_update_reply_markup()
        self.assertEqual(reply_markup["inline_keyboard"][0][0]["text"], "🔄 立即更新")

    def test_telegram_get_updates_accepts_callback_queries(self):
        calls = []
        original = m.telegram_api_request

        def fake_request(cfg, method, payload, timeout_seconds=20):
            calls.append((method, payload, timeout_seconds))
            return {"ok": True, "result": []}

        try:
            m.telegram_api_request = fake_request
            self.assertEqual(m.telegram_get_updates(m.Config(telegram_bot_token="123:token"), offset=10), [])
        finally:
            m.telegram_api_request = original

        _method, payload, _timeout_seconds = calls[0]
        self.assertIn("callback_query", m.json.loads(payload["allowed_updates"]))

    def test_answer_telegram_callback_payload(self):
        calls = []
        original = m.telegram_api_request

        def fake_request(cfg, method, payload, timeout_seconds=20):
            calls.append((method, payload, timeout_seconds))
            return {"ok": True, "result": True}

        try:
            m.telegram_api_request = fake_request
            m.answer_telegram_callback(m.Config(telegram_bot_token="123:token"), "cb-1", "开始更新")
        finally:
            m.telegram_api_request = original

        self.assertEqual(calls[0][0], "answerCallbackQuery")
        self.assertEqual(calls[0][1]["callback_query_id"], "cb-1")
        self.assertEqual(calls[0][1]["text"], "开始更新")

    def test_update_status_message_shows_semantic_version_and_commit(self):
        message = m.build_update_status_message(
            {
                "local_app_version": "0.2.0",
                "remote_app_version": "0.2.1",
                "local_version": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "remote_version": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "ref": "main",
                "has_update": True,
                "error": "",
            },
            dt.timezone.utc,
        )

        self.assertIn("当前版本：v0.2.0 (aaaaaaaa)", message)
        self.assertIn("远端版本：v0.2.1 (bbbbbbbb)", message)

    def test_update_triggered_message_shows_semantic_version_arrow(self):
        message = m.build_update_triggered_message(
            {
                "local_app_version": "0.2.0",
                "remote_app_version": "0.2.1",
                "local_version": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "remote_version": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
            "/var/log/sub2api-monitor/update-from-telegram.log",
            dt.timezone.utc,
        )

        self.assertIn("v0.2.0 (aaaaaaaa) → v0.2.1 (bbbbbbbb)", message)

    def test_release_version_helpers(self):
        self.assertEqual(
            m.remote_raw_base_url("https://github.com/jiwen77/sub2api-monitor.git", "main"),
            "https://raw.githubusercontent.com/jiwen77/sub2api-monitor/main",
        )
        self.assertEqual(
            m.remote_raw_base_url("git@github.com:jiwen77/sub2api-monitor.git", "release/v1"),
            "https://raw.githubusercontent.com/jiwen77/sub2api-monitor/release%2Fv1",
        )
        self.assertEqual(m.display_release_version("0.2.0", "a" * 40), "v0.2.0 (aaaaaaaa)")
        self.assertEqual(m.display_release_version("", "b" * 40), "bbbbbbbb")
        self.assertEqual(
            m.remote_raw_base_url("https://github.com/jiwen77/sub2api-monitor.git", "6b23b66a706c49f67a41965075d6f809b4da1f0a"),
            "https://raw.githubusercontent.com/jiwen77/sub2api-monitor/6b23b66a706c49f67a41965075d6f809b4da1f0a",
        )

    def test_update_status_reads_remote_app_version_at_remote_commit(self):
        class FakeMonitor(m.Monitor):
            def __init__(self, cfg):
                super().__init__(cfg, dry_run=True)

        original_current_install = m.current_install_version
        original_current_app = m.current_app_version
        original_remote_git = m.remote_git_version
        original_remote_app = m.remote_app_version_for_ref
        remote_app_calls = []

        try:
            m.current_install_version = lambda: "a" * 40
            m.current_app_version = lambda: "0.2.0"
            m.remote_git_version = lambda repo_url, ref: "b" * 40

            def fake_remote_app(repo_url, ref):
                remote_app_calls.append((repo_url, ref))
                return "0.2.1"

            m.remote_app_version_for_ref = fake_remote_app
            status = FakeMonitor(m.Config()).check_update_status()
        finally:
            m.current_install_version = original_current_install
            m.current_app_version = original_current_app
            m.remote_git_version = original_remote_git
            m.remote_app_version_for_ref = original_remote_app

        self.assertEqual(status["remote_app_version"], "0.2.1")
        self.assertEqual(remote_app_calls[0][1], "b" * 40)

    def test_change_alert_can_hide_summary_and_abnormal_list(self):
        cfg = m.Config()
        changed = {
            "id": 108,
            "platform": "openai",
            "type": "oauth",
            "plan": "plus",
            "status": "active",
            "normal": True,
            "schedulable": True,
            "rate_limited": False,
            "overloaded": False,
            "temp_unschedulable": False,
            "expired": False,
            "email": "new@example.com",
            "_previous": {
                "id": 108,
                "platform": "openai",
                "type": "oauth",
                "plan": "plus",
                "status": "active",
                "normal": False,
                "schedulable": True,
                "rate_limited": True,
                "rate_limit_reset_at": "2026-06-05 12:49:00+00",
            },
        }
        abnormal = {
            "id": 101,
            "platform": "openai",
            "type": "oauth",
            "plan": "plus",
            "status": "error",
            "normal": False,
            "schedulable": True,
            "rate_limited": False,
            "error_message": "Token revoked (401)",
        }

        message = m.build_account_message(
            [changed, abnormal],
            [changed],
            [],
            [],
            cfg,
            title="test",
            include_summary=False,
            include_abnormal=False,
        )

        self.assertIn("本次变化", message)
        self.assertIn("#108", message)
        self.assertNotIn("账号概览", message)
        self.assertNotIn("当前需要关注", message)
        self.assertNotIn("#101", message)

    def test_account_message_can_include_all_accounts(self):
        cfg = m.Config()
        normal = {
            "id": 108,
            "platform": "openai",
            "type": "oauth",
            "plan": "plus",
            "status": "active",
            "normal": True,
            "schedulable": True,
            "rate_limited": False,
            "overloaded": False,
            "temp_unschedulable": False,
            "expired": False,
            "email": "normal@example.com",
            "groups": [
                {"id": 1, "name": "default", "status": "active"},
                {"id": 2, "name": "claude-code", "status": "active"},
                {"id": 3, "name": "vip", "status": "disabled"},
            ],
        }
        abnormal = {
            "id": 101,
            "platform": "openai",
            "type": "oauth",
            "plan": "plus",
            "status": "error",
            "normal": False,
            "schedulable": True,
            "rate_limited": False,
            "error_message": "Token revoked (401)",
        }

        message = m.build_account_message(
            [normal, abnormal],
            [],
            [],
            [],
            cfg,
            title="test",
            include_abnormal=False,
            include_all=True,
            clamp=False,
        )

        self.assertIn("全部账号状态（2 个）", message)
        self.assertIn("#108", message)
        self.assertIn("#101", message)
        self.assertIn("所属分组：default、claude-code、vip(disabled)", message)
        self.assertIn("正常", message)
        self.assertNotIn("当前需要关注", message)

    def test_status_summary_includes_non_redundant_quota_totals(self):
        cfg = m.Config()
        rows = [
            {
                "id": 108,
                "platform": "openai",
                "plan": "plus",
                "status": "active",
                "normal": True,
                "schedulable": True,
                "rate_limited": False,
                "codex_5h_used_percent": 25,
                "codex_7d_used_percent": 50,
            },
            {
                "id": 109,
                "platform": "openai",
                "plan": "plus",
                "status": "active",
                "normal": False,
                "schedulable": False,
                "rate_limited": True,
                "codex_5h_used_percent": 100,
                "codex_7d_used_percent": 36,
            },
            {
                "id": 110,
                "platform": "openai",
                "plan": "free",
                "status": "active",
                "normal": True,
                "schedulable": True,
                "rate_limited": False,
                "codex_5h_used_percent": 0,
                "codex_7d_used_percent": 100,
            },
            {
                "id": 111,
                "platform": "openai",
                "plan": "apikey",
                "status": "active",
                "normal": True,
                "schedulable": True,
                "rate_limited": False,
            },
            {
                "id": 112,
                "platform": "openai",
                "plan": "apikey",
                "status": "error",
                "normal": False,
                "schedulable": True,
                "rate_limited": False,
            },
            {
                "id": 113,
                "platform": "openai",
                "plan": "apikey",
                "status": "active",
                "normal": False,
                "schedulable": False,
                "rate_limited": False,
            },
        ]

        message = m.build_account_message(rows, [], [], [], cfg, title="test", include_abnormal=False, clamp=False)

        self.assertIn("账号概览", message)
        self.assertIn("🟡 <code>openai/plus</code> 1/2 · 限流1", message)
        self.assertIn("🟡 <code>openai/apikey</code> 1/3 · 异常1 · 不可调度1", message)
        self.assertIn("剩余额度汇总", message)
        self.assertIn("<code>openai/plus</code>\n  <code>5h 75%</code> · <code>7d 114%</code>", message)
        self.assertIn("<code>openai/free</code>\n  <code>5h 100%</code> · <code>7d 0%</code>", message)
        self.assertNotIn("<code>openai/plus</code> 可用 1/2", message)
        self.assertNotIn("<code>openai/apikey</code>\n  <code>5h", message)

    def test_quota_summary_keeps_rate_limited_but_excludes_unusable_accounts(self):
        rows = [
            {
                "id": 108,
                "platform": "openai",
                "plan": "plus",
                "status": "active",
                "normal": True,
                "schedulable": True,
                "rate_limited": False,
                "codex_5h_used_percent": 25,
                "codex_7d_used_percent": 50,
            },
            {
                "id": 109,
                "platform": "openai",
                "plan": "plus",
                "status": "active",
                "normal": False,
                "schedulable": False,
                "rate_limited": True,
                "codex_5h_used_percent": 100,
                "codex_7d_used_percent": 36,
            },
            {
                "id": 110,
                "platform": "openai",
                "plan": "plus",
                "status": "error",
                "normal": False,
                "schedulable": True,
                "rate_limited": False,
                "codex_5h_used_percent": 1,
                "codex_7d_used_percent": 1,
            },
            {
                "id": 111,
                "platform": "openai",
                "plan": "plus",
                "status": "active",
                "normal": False,
                "schedulable": False,
                "rate_limited": False,
                "codex_5h_used_percent": 2,
                "codex_7d_used_percent": 2,
            },
            {
                "id": 112,
                "platform": "openai",
                "plan": "plus",
                "status": "active",
                "normal": False,
                "schedulable": True,
                "rate_limited": False,
                "expired": True,
                "codex_5h_used_percent": 3,
                "codex_7d_used_percent": 3,
            },
        ]

        self.assertEqual(
            m.format_quota_summary_lines(rows),
            ["<code>openai/plus</code>\n  <code>5h 75%</code> · <code>7d 114%</code>"],
        )

    def test_account_group_summary_limits_many_groups_cleanly(self):
        row = {
            "groups": [
                {"id": idx, "name": f"group-{idx}", "status": "active"}
                for idx in range(1, 8)
            ]
        }

        self.assertEqual(
            m.account_groups_summary(row, max_groups=3),
            "group-1、group-2、group-3 等 7 个",
        )

    def test_group_membership_digest_migration_does_not_alert_all_accounts_once(self):
        previous = {
            "id": 108,
            "platform": "openai",
            "type": "oauth",
            "plan": "plus",
            "status": "active",
            "normal": True,
            "schedulable": True,
            "rate_limited": False,
            "rate_limit_reset_at": None,
            "overloaded": False,
            "overload_until": None,
            "temp_unschedulable": False,
            "temp_unschedulable_until": None,
            "temp_unschedulable_reason": "",
            "expired": False,
            "expires_at": None,
            "error_message_hash": "",
        }
        current = dict(previous)
        current["groups"] = [{"id": 1, "name": "default", "status": "active"}]

        self.assertTrue(m.account_digests_equal(previous, current))

        previous_after_migration = dict(current)
        changed = dict(current)
        changed["groups"] = [{"id": 2, "name": "vip", "status": "active"}]
        self.assertFalse(m.account_digests_equal(previous_after_migration, changed))

    def test_accounts_list_message_is_not_health_overview(self):
        cfg = m.Config()
        message = m.build_accounts_list_message(
            [
                {
                    "id": 108,
                    "platform": "openai",
                    "plan": "plus",
                    "status": "active",
                    "normal": True,
                    "schedulable": True,
                    "rate_limited": False,
                    "overloaded": False,
                    "temp_unschedulable": False,
                    "expired": False,
                    "groups": [{"id": 1, "name": "default", "status": "active"}],
                }
            ],
            cfg,
        )

        self.assertIn("账号清单（1 个）", message)
        self.assertIn("所属分组：default", message)
        self.assertNotIn("账号概览", message)
        self.assertNotIn("当前需要关注", message)

    def test_groups_message_summarizes_memberships_and_attention_items(self):
        cfg = m.Config()
        rows = [
            {
                "id": 108,
                "platform": "openai",
                "plan": "plus",
                "status": "active",
                "normal": True,
                "schedulable": True,
                "rate_limited": False,
                "overloaded": False,
                "temp_unschedulable": False,
                "expired": False,
                "groups": [{"id": 1, "name": "default", "status": "active"}],
            },
            {
                "id": 101,
                "platform": "openai",
                "plan": "plus",
                "status": "error",
                "normal": False,
                "schedulable": True,
                "rate_limited": False,
                "error_message": "Token revoked (401)",
                "groups": [
                    {"id": 1, "name": "default", "status": "active"},
                    {"id": 2, "name": "vip", "status": "active"},
                ],
            },
        ]

        message = m.build_groups_message(rows, cfg)

        self.assertIn("分组概览", message)
        self.assertIn("<code>default</code> 1/2", message)
        self.assertIn("<code>vip</code> 0/1", message)
        self.assertIn("需要关注的账号（去重，1 个）", message)
        self.assertEqual(message.count("#101"), 1)
        self.assertIn("所属分组：default、vip", message)

    def test_groups_message_breaks_health_and_quota_down_by_account_type(self):
        cfg = m.Config()
        rows = [
            {
                "id": 108,
                "platform": "openai",
                "plan": "plus",
                "status": "active",
                "normal": True,
                "schedulable": True,
                "rate_limited": False,
                "codex_5h_used_percent": 25,
                "codex_7d_used_percent": 50,
                "groups": [{"id": 1, "name": "default", "status": "active"}],
            },
            {
                "id": 109,
                "platform": "openai",
                "plan": "plus",
                "status": "active",
                "normal": False,
                "schedulable": False,
                "rate_limited": True,
                "codex_5h_used_percent": 100,
                "codex_7d_used_percent": 36,
                "groups": [{"id": 1, "name": "default", "status": "active"}],
            },
            {
                "id": 110,
                "platform": "openai",
                "plan": "apikey",
                "status": "active",
                "normal": True,
                "schedulable": True,
                "rate_limited": False,
                "groups": [{"id": 1, "name": "default", "status": "active"}],
            },
            {
                "id": 111,
                "platform": "openai",
                "plan": "apikey",
                "status": "active",
                "normal": False,
                "schedulable": False,
                "rate_limited": False,
                "groups": [{"id": 1, "name": "default", "status": "active"}],
            },
        ]

        message = m.build_groups_message(rows, cfg)

        self.assertIn("<code>default</code> 2/4 · 限流1 · 不可调度1", message)
        self.assertIn("  <code>openai/apikey</code> 1/2 · 不可调度1", message)
        self.assertIn("  <code>openai/plus</code> 1/2 · 限流1", message)
        self.assertIn("    剩余额度 <code>5h 75%</code> · <code>7d 114%</code>", message)
        self.assertNotIn("APIKey可用", message)

    def test_daily_message_keeps_yesterday_and_today_details_separate(self):
        cfg = m.Config()
        yesterday = {
            "summary": {
                "requests": 10,
                "input_tokens": 1_000_000,
                "output_tokens": 200_000,
                "cache_tokens": 3_000_000,
                "total_tokens": 4_200_000,
                "total_cost": "12.345678",
                "avg_duration_ms": "100.1",
                "avg_first_token_ms": "20.2",
            },
            "by_plan": [
                {"platform": "openai", "plan": "plus", "requests": 7, "total_tokens": 3_000_000},
            ],
            "top_models": [
                {"model": "gpt-5.5", "requests": 7, "total_tokens": 3_000_000},
            ],
        }
        today = {
            "summary": {
                "requests": 5,
                "input_tokens": 500_000,
                "output_tokens": 100_000,
                "cache_tokens": 2_000_000,
                "total_tokens": 2_600_000,
                "total_cost": "8.000000",
                "avg_duration_ms": "90.5",
                "avg_first_token_ms": "18.0",
            },
            "by_plan": [
                {"platform": "openai", "plan": "team", "requests": 5, "total_tokens": 2_600_000},
            ],
            "top_models": [
                {"model": "gpt-5.4", "requests": 5, "total_tokens": 2_600_000},
            ],
        }

        message = m.build_daily_message(
            dt.date(2026, 6, 1),
            yesterday,
            dt.date(2026, 6, 2),
            today,
            cfg,
            dt.datetime(2026, 6, 2, 12, 42, 56, tzinfo=dt.timezone.utc),
        )

        self.assertIn("🌙 昨日 2026-06-01", message)
        self.assertIn("☀️ 今日 2026-06-02 截至当前", message)
        self.assertIn("\n━━━━━━━━━━━━\n", message)
        self.assertNotIn("\n\n👥", m.render_for_terminal(message))
        self.assertNotIn("\n\n🤖", m.render_for_terminal(message))
        self.assertNotIn("昨日按账号类型", message)
        self.assertNotIn("今日按账号类型", message)
        self.assertEqual(message.count("👥"), 2)
        self.assertEqual(message.count("🤖"), 2)
        self.assertIn("用量 <code>2.60M</code> · 请求 <code>5</code> · 成本 <code>8.000000</code>", message)
        self.assertIn("输入/输出/缓存 <code>500.00K</code> / <code>100.00K</code> / <code>2.00M</code>", message)
        self.assertIn("平均 <code>90.5ms</code> · 首Token <code>18.0ms</code>", message)
        self.assertNotIn("Tokens <code>", message)
        self.assertNotIn("First token", message)
        yesterday_summary = message.index("🌙 昨日 2026-06-01")
        yesterday_by_plan = message.index("按账号类型")
        yesterday_top_models = message.index("Top 模型")
        today_summary = message.index("☀️ 今日 2026-06-02 截至当前")
        divider = message.index("━━━━━━━━━━━━")
        today_by_plan = message.index("按账号类型", today_summary)
        today_top_models = message.index("Top 模型", today_summary)
        self.assertLess(yesterday_summary, yesterday_by_plan)
        self.assertLess(yesterday_by_plan, yesterday_top_models)
        self.assertLess(yesterday_top_models, divider)
        self.assertLess(divider, today_summary)
        self.assertLess(today_summary, today_by_plan)
        self.assertLess(today_by_plan, today_top_models)
        self.assertIn("  • <code>openai/team</code> 2.60M · 5 req", message)
        self.assertIn("  • <code>gpt-5.4</code> 2.60M · 5 req", message)


if __name__ == "__main__":
    unittest.main()

class TelegramCommandTests(unittest.TestCase):
    def test_normalize_telegram_command(self):
        self.assertEqual(m.normalize_telegram_command('/status@SomeBot now'), '/status')
        self.assertEqual(m.normalize_telegram_command('/STATUS_ALL@SomeBot now'), '/status_all')
        self.assertEqual(m.normalize_telegram_command('/DAILY'), '/daily')

    def test_chat_authorization_defaults_to_notification_chat(self):
        cfg = m.Config(telegram_chat_id='123')
        self.assertTrue(m.telegram_chat_allowed(cfg, '123'))
        self.assertFalse(m.telegram_chat_allowed(cfg, '456'))

    def test_chat_authorization_allows_explicit_list(self):
        cfg = m.Config(telegram_chat_id='123', telegram_allowed_chat_ids='456, 789')
        self.assertFalse(m.telegram_chat_allowed(cfg, '123'))
        self.assertTrue(m.telegram_chat_allowed(cfg, '456'))
        self.assertTrue(m.telegram_chat_allowed(cfg, '789'))

    def test_accounts_command_sends_account_list(self):
        class FakeMonitor(m.Monitor):
            def __init__(self, cfg):
                self.sent = []
                super().__init__(cfg, dry_run=True)

            def current_account_rows(self):
                return [
                    {
                        "id": 108,
                        "platform": "openai",
                        "type": "oauth",
                        "plan": "plus",
                        "status": "active",
                        "normal": True,
                        "schedulable": True,
                        "rate_limited": False,
                        "overloaded": False,
                        "temp_unschedulable": False,
                        "expired": False,
                    },
                    {
                        "id": 101,
                        "platform": "openai",
                        "type": "oauth",
                        "plan": "plus",
                        "status": "error",
                        "normal": False,
                        "schedulable": True,
                        "rate_limited": False,
                    },
                ]

            def send(self, text, chat_id=None):
                self.sent.append((text, chat_id))

        with tempfile.TemporaryDirectory() as tmp:
            mon = FakeMonitor(m.Config(state_file=f"{tmp}/state.json", telegram_chat_id="123"))
            mon.handle_telegram_update({"message": {"chat": {"id": "123"}, "text": "/accounts"}})

        self.assertEqual(len(mon.sent), 1)
        text, chat_id = mon.sent[0]
        self.assertEqual(chat_id, "123")
        self.assertIn("账号清单（2 个）", text)
        self.assertIn("#108", text)
        self.assertIn("#101", text)
        self.assertNotIn("账号概览", text)

    def test_groups_command_sends_group_overview(self):
        class FakeMonitor(m.Monitor):
            def __init__(self, cfg):
                self.sent = []
                super().__init__(cfg, dry_run=True)

            def current_account_rows(self):
                return [
                    {
                        "id": 101,
                        "platform": "openai",
                        "plan": "plus",
                        "status": "error",
                        "normal": False,
                        "schedulable": True,
                        "rate_limited": False,
                        "groups": [{"id": 1, "name": "default", "status": "active"}],
                    }
                ]

            def send(self, text, chat_id=None):
                self.sent.append((text, chat_id))

        with tempfile.TemporaryDirectory() as tmp:
            mon = FakeMonitor(m.Config(state_file=f"{tmp}/state.json", telegram_chat_id="123"))
            mon.handle_telegram_update({"message": {"chat": {"id": "123"}, "text": "/groups"}})

        self.assertEqual(len(mon.sent), 1)
        text, chat_id = mon.sent[0]
        self.assertEqual(chat_id, "123")
        self.assertIn("分组概览", text)
        self.assertIn("default", text)
        self.assertIn("#101", text)

    def test_update_command_sends_button_when_remote_version_is_newer(self):
        class FakeMonitor(m.Monitor):
            def __init__(self, cfg):
                self.sent = []
                super().__init__(cfg, dry_run=True)

            def check_update_status(self):
                return {
                    "local_version": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "remote_version": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "has_update": True,
                    "error": "",
                }

            def send(self, text, chat_id=None, reply_markup=None):
                self.sent.append((text, chat_id, reply_markup))

        with tempfile.TemporaryDirectory() as tmp:
            mon = FakeMonitor(m.Config(state_file=f"{tmp}/state.json", telegram_chat_id="123"))
            mon.handle_telegram_update({"message": {"chat": {"id": "123"}, "text": "/update"}})

        self.assertEqual(len(mon.sent), 1)
        text, chat_id, reply_markup = mon.sent[0]
        self.assertEqual(chat_id, "123")
        self.assertIn("发现新版本", text)
        self.assertIn("aaaaaaaa", text)
        self.assertIn("bbbbbbbb", text)
        self.assertEqual(reply_markup["inline_keyboard"][0][0]["text"], "🔄 立即更新")
        self.assertEqual(reply_markup["inline_keyboard"][0][0]["callback_data"], "sub2api:update:run")

    def test_update_callback_rechecks_then_triggers_update(self):
        class FakeMonitor(m.Monitor):
            def __init__(self, cfg):
                self.sent = []
                self.answered = []
                self.triggered = False
                super().__init__(cfg, dry_run=True)

            def check_update_status(self):
                return {
                    "local_version": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "remote_version": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "has_update": True,
                    "error": "",
                }

            def trigger_self_update(self):
                self.triggered = True
                return "/var/log/sub2api-monitor/update-from-telegram.log"

            def answer_callback_query(self, callback_query_id, text=""):
                self.answered.append((callback_query_id, text))

            def send(self, text, chat_id=None, reply_markup=None):
                self.sent.append((text, chat_id, reply_markup))

        with tempfile.TemporaryDirectory() as tmp:
            mon = FakeMonitor(m.Config(state_file=f"{tmp}/state.json", telegram_chat_id="123"))
            mon.handle_telegram_update({
                "callback_query": {
                    "id": "cb-1",
                    "data": "sub2api:update:run",
                    "message": {"chat": {"id": "123"}},
                }
            })

        self.assertTrue(mon.triggered)
        self.assertEqual(mon.answered, [("cb-1", "开始更新")])
        self.assertIn("已触发更新", mon.sent[0][0])
        self.assertEqual(mon.sent[0][1], "123")
