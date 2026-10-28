"""粘贴 Cookie / BDUSS 的解析测试 (完全离线)。

覆盖三种粘贴形态、缺失登录态、以及 BDUSS_BFESS 兼容分支。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netdisk.credentials import (  # noqa: E402
    COOKIE_DOMAIN,
    CredentialError,
    build_baidu_credentials,
    parse_cookie_text,
)


class ParseCookieTextTests(unittest.TestCase):
    def test_parses_request_header_line(self):
        pairs = parse_cookie_text("BDUSS=abc123; STOKEN=def456; BAIDUID=zzz; PANWEB=1")
        self.assertEqual(list(pairs), ["BDUSS", "STOKEN", "BAIDUID", "PANWEB"])
        self.assertEqual(pairs["BDUSS"], "abc123")
        self.assertEqual(pairs["PANWEB"], "1")

    def test_strips_cookie_prefix_and_drops_attributes(self):
        pairs = parse_cookie_text("Cookie: BDUSS=abc; Path=/; Domain=.baidu.com; Secure; HttpOnly")
        self.assertEqual(pairs, {"BDUSS": "abc"})

    def test_parses_multiline_tabbed_table_paste(self):
        raw = "BDUSS\tvalue-bduss\t.baidu.com\t/\nSTOKEN\tvalue-stoken\t.baidu.com\t/"
        self.assertEqual(parse_cookie_text(raw), {"BDUSS": "value-bduss", "STOKEN": "value-stoken"})

    def test_multiline_name_value_pairs(self):
        pairs = parse_cookie_text("BDUSS=abc\nSTOKEN=def\n")
        self.assertEqual(pairs, {"BDUSS": "abc", "STOKEN": "def"})

    def test_duplicate_names_take_last_value(self):
        self.assertEqual(parse_cookie_text("BDUSS=old; BDUSS=new")["BDUSS"], "new")

    def test_quoted_values_are_unwrapped(self):
        self.assertEqual(parse_cookie_text('BDUSS="abc"')["BDUSS"], "abc")

    def test_empty_input_is_empty_mapping(self):
        for raw in ("", "   ", None):
            self.assertEqual(parse_cookie_text(raw), {})


class BuildCredentialsTests(unittest.TestCase):
    def test_full_cookie_header_keeps_every_pair(self):
        creds = build_baidu_credentials("BDUSS=abc; STOKEN=def; BAIDUID=zzz")
        self.assertEqual(creds.bduss, "abc")
        self.assertEqual(creds.cookie_names, ["BDUSS", "STOKEN", "BAIDUID"])
        self.assertEqual(creds.cookie_names.count("BDUSS"), 1)
        for cookie in creds.cookies:
            self.assertEqual(cookie["domain"], COOKIE_DOMAIN)
            self.assertEqual(cookie["path"], "/")
        self.assertEqual(creds.notes, [])
        self.assertEqual(creds.alt_bduss, "")

    def test_missing_stoken_is_reported_but_usable(self):
        creds = build_baidu_credentials("BDUSS=abc")
        self.assertEqual(creds.bduss, "abc")
        self.assertTrue(any("STOKEN" in note for note in creds.notes))

    def test_bduss_bfess_only_is_accepted(self):
        creds = build_baidu_credentials("BDUSS_BFESS=bfess-value; STOKEN=def")
        self.assertEqual(creds.bduss, "bfess-value")
        self.assertTrue(any("BDUSS_BFESS" in note for note in creds.notes))

    def test_bfess_is_kept_as_retry_alternative(self):
        creds = build_baidu_credentials("BDUSS=main; BDUSS_BFESS=alt")
        self.assertEqual(creds.bduss, "main")
        self.assertEqual(creds.alt_bduss, "alt")

    def test_anonymous_only_cookies_explain_what_is_missing(self):
        with self.assertRaises(CredentialError) as ctx:
            build_baidu_credentials("BAIDUID=zzz; csrfToken=abc")
        self.assertIn("BDUSS", str(ctx.exception))
        self.assertIn("pan.baidu.com", str(ctx.exception))

    def test_unparsable_input_asks_for_a_cookie(self):
        with self.assertRaises(CredentialError) as ctx:
            build_baidu_credentials("这不是 Cookie")
        self.assertIn("Cookie", str(ctx.exception))

    def test_bare_token_is_accepted_for_backwards_compatibility(self):
        token = "a" * 60
        creds = build_baidu_credentials(token)
        self.assertEqual(creds.bduss, token)
        self.assertEqual(creds.cookie_names, ["BDUSS"])

    def test_empty_values_are_not_stored_as_cookies(self):
        creds = build_baidu_credentials("BDUSS=abc; EMPTY=; STOKEN=def")
        self.assertNotIn("EMPTY", creds.cookie_names)


if __name__ == "__main__":
    unittest.main()
