"""Enterprise private-tenant accounts and the 401 auto-retire path.

Covers three things that are easy to regress:

- an edge-level ``401 Authorization Required`` body retires the account after
  three consecutive rejections (and any success resets the streak);
- a CodeBuddy/desktop credential (``account`` + ``auth``) is imported as one
  account even when the file also carries a token-less ``accounts`` cache;
- an enterprise credential resolves to the CN realm and derives its private
  chat/billing host from ``domain`` instead of using the public endpoint.
"""
import base64
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts


def _b64(obj):
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode("utf-8")).rstrip(b"=").decode("ascii")


def _jwt(claims):
    return "%s.%s.%s" % (_b64({"alg": "none"}), _b64(claims), _b64({"sig": "x"}))


def _account(directory, **extra):
    data = {"uid": "enterpriseaccount", "accessToken": "token", "realm": "cn"}
    data.update(extra)
    return wb_accounts.Account(
        data, path=os.path.join(directory, "enterpriseaccount.json"))


class AuthRejectTests(unittest.TestCase):
    BODY = b'<html><head><title>401 Authorization Required</title></head></html>'

    def test_three_consecutive_rejections_disable_and_persist(self):
        with tempfile.TemporaryDirectory(prefix="auth401-") as directory:
            account = _account(directory)
            self.assertFalse(account.note_auth_rejected(b'{"code": 401}'))
            self.assertEqual(account.auth401_fails, 0)
            self.assertFalse(account.note_auth_rejected(self.BODY))
            self.assertFalse(account.note_auth_rejected(self.BODY))
            self.assertTrue(account.note_auth_rejected(self.BODY))
            self.assertFalse(account.enabled)
            self.assertFalse(account.tasks_enabled)
            with open(account.path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertIs(saved["enabled"], False)
            self.assertIs(saved["tasksEnabled"], False)

    def test_success_resets_the_streak(self):
        with tempfile.TemporaryDirectory(prefix="auth401-") as directory:
            account = _account(directory)
            account.note_auth_rejected(self.BODY)
            account.note_auth_rejected(self.BODY)
            account.note_success()
            self.assertEqual(account.auth401_fails, 0)


class ImportShapeTests(unittest.TestCase):
    def test_account_and_auth_win_over_profile_cache(self):
        blob = {
            "account": {"uid": "u1", "enterpriseId": "tenant"},
            "auth": {"accessToken": "a.b.c", "refreshToken": "r",
                     "domain": "tenant.copilot.qq.com"},
            "accounts": [{"uid": "u1"}],
        }
        rows, problem = wb_accounts._coerce_account_rows(blob)
        self.assertEqual(problem, "")
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0], blob)

    def test_enterprise_credential_gets_cn_and_private_host(self):
        token = _jwt({
            "sub": "enterprise-uid",
            "iss": "https://tenant.copilot.qq.com/auth/realms/copilot",
            "exp": 4102444800,
        })
        row = {
            "account": {"uid": "enterprise-uid", "nickname": "N",
                        "enterpriseId": "tenant"},
            "auth": {"accessToken": token, "refreshToken": "r",
                     "domain": "tenant.copilot.qq.com"},
            "accounts": [{"uid": "enterprise-uid"}],
        }
        rows, problem = wb_accounts._coerce_account_rows(row)
        self.assertEqual(problem, "")
        normalised = wb_accounts.normalise_import_row(rows[0])
        self.assertEqual(normalised["realm"], "cn")
        self.assertEqual(normalised["enterpriseId"], "tenant")
        self.assertEqual(normalised["domain"], "tenant.copilot.qq.com")
        self.assertEqual(normalised["chatBase"], "https://tenant.copilot.qq.com")
        self.assertEqual(normalised["billingBase"], "https://tenant.copilot.qq.com")


if __name__ == "__main__":
    unittest.main()
