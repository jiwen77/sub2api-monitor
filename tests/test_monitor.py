import datetime as dt
import tempfile
import unittest

import sub2api_monitor as m


class PredicateTests(unittest.TestCase):
    def test_status_ranges(self):
        ranges = m.parse_allowed_status("429,500-599")
        self.assertTrue(m.status_allowed(429, ranges))
        self.assertTrue(m.status_allowed(503, ranges))
        self.assertFalse(m.status_allowed(401, ranges))

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
        self.assertIn("当前用量：5h 100.0% · 7d 46.0%", message)
        self.assertNotIn("当前需要关注", message)

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
                [{"text": "立即更新", "callback_data": "sub2api:update:run"}],
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

        self.assertIn("今日按账号类型", message)
        self.assertIn("今日 Top 模型", message)
        yesterday_summary = message.index("昨日 2026-06-01")
        yesterday_by_plan = message.index("昨日按账号类型")
        yesterday_top_models = message.index("昨日 Top 模型")
        today_summary = message.index("今日 2026-06-02 截至当前")
        today_by_plan = message.index("今日按账号类型")
        today_top_models = message.index("今日 Top 模型")
        self.assertLess(yesterday_summary, yesterday_by_plan)
        self.assertLess(yesterday_by_plan, yesterday_top_models)
        self.assertLess(yesterday_top_models, today_summary)
        self.assertLess(today_summary, today_by_plan)
        self.assertLess(today_by_plan, today_top_models)
        self.assertIn("Input <code>500.00K</code> · Output <code>100.00K</code> · Cache <code>2.00M</code>", message)
        self.assertIn("Avg <code>90.5 ms</code> · First token <code>18.0 ms</code>", message)
        self.assertIn("<code>openai/team</code> 2.60M tokens · 5 req", message)
        self.assertIn("<code>gpt-5.4</code> 2.60M tokens · 5 req", message)


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
        self.assertEqual(reply_markup["inline_keyboard"][0][0]["text"], "立即更新")
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
