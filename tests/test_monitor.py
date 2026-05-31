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


if __name__ == "__main__":
    unittest.main()
