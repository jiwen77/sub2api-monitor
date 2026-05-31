import datetime as dt
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

    def test_telegram_bot_commands_are_valid_for_api(self):
        for command in m.TELEGRAM_BOT_COMMANDS:
            self.assertRegex(command["command"], r"^[a-z0-9_]{1,32}$")
            self.assertGreaterEqual(len(command["description"]), 1)
            self.assertLessEqual(len(command["description"]), 256)

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


if __name__ == "__main__":
    unittest.main()

class TelegramCommandTests(unittest.TestCase):
    def test_normalize_telegram_command(self):
        self.assertEqual(m.normalize_telegram_command('/status@SomeBot now'), '/status')
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
