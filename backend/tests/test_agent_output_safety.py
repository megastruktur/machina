"""Focused tests for the shared output-safety projection (act 02).

Acceptance (BRIEF criterion 1): synthetic nested, JSON-escaped, multiline,
base64/header-style and large (> 4096 chars) secret-shaped token fixtures
(``sk-...``, ``Bearer ...``, ``password=...``, ``api_key=...``, PEM blocks,
JWTs, URL userinfo credentials) do NOT survive the receipt, persisted
transcript, or model-history projections. Redaction runs BEFORE any size
bound, traverses nested dicts/lists/JSON-encoded string payloads, and the
explicit bounds apply after redaction so a truncated tail never splits or
leaks a partial secret prefix.

Pure unit layer: no database, no dispatcher, no fixtures with real
credential material (synthetic canary markers only).
"""

from __future__ import annotations

import json
import unittest

from mai.agents.output_safety import (
    MAX_PROJECTION_CHARS,
    REDACTED,
    bound_text,
    project_document,
    project_text,
    redact_document,
    redact_text,
    redact_values,
)

#: Synthetic canaries. Marker words only — never real credential material.
CANARY = "fxt-canary-0123456789abcdefghijklmnopqrstuvwxyz"
SK_TOKEN = "sk-" + "A" * 30
BIG_SK_TOKEN = "sk-" + "B" * 5000  # large (> 4096 chars) token fixture
BEARER_TOKEN = "Bearer " + "G" * 40
BASIC_TOKEN = "Authorization: Basic " + "aGk6c2VjcmV0" + "Z" * 40
PEM_BLOCK = (
    "-----BEGIN PRIVATE KEY-----\n"
    + "C" * 200
    + "\n-----END PRIVATE KEY-----"
)
JWT_TOKEN = "eyJ" + "D" * 40 + "." + "E" * 40 + "." + "F" * 20
BIG_JWT_TOKEN = "eyJ" + "D" * 2100 + "." + "E" * 2100 + "." + "F" * 100
USERINFO_URL = "postgres://deploy:" + CANARY + "@db.internal:5432/mai"


def _canaries_absent(text: str) -> None:
    for secret in (
        CANARY,
        SK_TOKEN,
        BIG_SK_TOKEN,
        BEARER_TOKEN,
        BASIC_TOKEN,
        PEM_BLOCK,
        JWT_TOKEN,
        BIG_JWT_TOKEN,
        USERINFO_URL,
    ):
        assert secret not in text, f"leaked: {secret[:40]!r}"


class TestRedactText(unittest.TestCase):
    """The pattern + resolved-value layers over one flat string."""

    def test_api_key_shapes_redacted(self) -> None:
        for text in (
            f"key={SK_TOKEN}",
            f'"api_key": "{SK_TOKEN}"',
            f"token {SK_TOKEN} trailing",
            BIG_SK_TOKEN,
        ):
            out = redact_text(text)
            self.assertIn(REDACTED, out)
            self.assertNotIn("A" * 30, out)
            self.assertNotIn(BIG_SK_TOKEN, out)

    def test_bearer_and_basic_header_styles_redacted(self) -> None:
        for text in (
            f"authorization: {BEARER_TOKEN}",
            f"AUTHORIZATION: basic {BASIC_TOKEN.split()[-1]}",
            f"{'bEaReR'} " + "G" * 40,
        ):
            out = redact_text(text)
            self.assertIn(REDACTED, out)
            _canaries_absent(out + " " + "G" * 40)

    def test_key_value_credentials_redacted_with_context_kept(self) -> None:
        out = redact_text(f"password={CANARY} and api_key: {CANARY}")
        self.assertIn("password=", out)
        self.assertIn("api_key: ", out)
        self.assertNotIn(CANARY, out)

    def test_json_quoted_key_value_redacted_and_parseable(self) -> None:
        raw = json.dumps({"password": CANARY, "ok": 1})
        out = redact_text(raw)
        self.assertNotIn(CANARY, out)
        self.assertEqual(json.loads(out)["ok"], 1)

    def test_multiline_pem_block_redacted(self) -> None:
        out = redact_text(f"cert:\n{PEM_BLOCK}\nend")
        self.assertIn(REDACTED, out)
        self.assertNotIn("BEGIN", out)
        self.assertNotIn("C" * 200, out)

    def test_jwt_redacted_including_oversized(self) -> None:
        for token in (JWT_TOKEN, BIG_JWT_TOKEN):
            out = redact_text(f"auth {token} end")
            self.assertIn(REDACTED, out)
            self.assertNotIn(token, out)

    def test_url_userinfo_password_redacted_user_kept(self) -> None:
        out = redact_text(f"connect {USERINFO_URL} now")
        self.assertIn("postgres://deploy:", out)
        self.assertNotIn(CANARY, out)

    def test_resolved_credential_json_escaped_forms_redacted(self) -> None:
        escaped = json.dumps(CANARY, ensure_ascii=True)[1:-1]
        out = redact_text('payload {"v": "' + escaped + '"}', [CANARY])
        self.assertNotIn(CANARY, out)
        self.assertIn(REDACTED, out)

    def test_clean_text_untouched(self) -> None:
        clean = '{"status": "succeeded", "exit_code": 0, "note": "ok"}'
        self.assertEqual(redact_text(clean), clean)


class TestRedactDocument(unittest.TestCase):
    """Recursive traversal of nested and JSON-encoded payloads."""

    def test_nested_dicts_lists_tuples_traversed(self) -> None:
        document = {"a": {"b": [f"token={CANARY}", ("k", SK_TOKEN)], "n": 3}}
        out = redact_document(document)
        _canaries_absent(json.dumps(out))
        self.assertEqual(out["a"]["b"][1], ["k", REDACTED])
        self.assertEqual(out["a"]["n"], 3)

    def test_json_encoded_string_payload_redacted(self) -> None:
        encoded = json.dumps({"password": CANARY, "jwt": JWT_TOKEN})
        out = redact_document({"encoded": encoded})
        assert isinstance(out, dict)
        text = out["encoded"]
        assert isinstance(text, str)
        self.assertNotIn(CANARY, text)
        self.assertNotIn(JWT_TOKEN, text)
        # the re-encoded payload stays a valid JSON document
        self.assertEqual(json.loads(text)["password"], REDACTED)

    def test_double_encoded_payload_redacted(self) -> None:
        inner = json.dumps({"api_key": SK_TOKEN})
        document = {"outer": json.dumps({"inner": inner})}
        out = json.dumps(redact_document(document))
        self.assertNotIn(SK_TOKEN, out)
        self.assertIn(REDACTED, out)

    def test_deep_nesting_redacts_without_crash(self) -> None:
        node: dict = {"leaf": f"password={CANARY}"}
        for _ in range(40):
            node = {"child": node}
        out = json.dumps(redact_document(node))
        self.assertNotIn(CANARY, out)

    def test_non_string_scalars_pass_through(self) -> None:
        document = {"n": 7, "b": True, "z": None, "f": 1.5}
        self.assertEqual(redact_document(document), document)


class TestProjectDocument(unittest.TestCase):
    """The durable receipt projection: redact, then bound."""

    def test_json_document_redacted_structure_preserved(self) -> None:
        raw = json.dumps(
            {
                "status": "succeeded",
                "nested": {"api_key": SK_TOKEN},
                "encoded": json.dumps({"password": CANARY}),
                "pem": PEM_BLOCK,
                "url": USERINFO_URL,
                "big": BIG_SK_TOKEN,
                "jwt": JWT_TOKEN,
                "bearer": BEARER_TOKEN,
                "basic": BASIC_TOKEN,
                "keep": {"n": 7, "ok": True},
            }
        )
        out = project_document(raw)
        assert isinstance(out, dict)
        _canaries_absent(json.dumps(out))
        self.assertEqual(out["keep"], {"n": 7, "ok": True})
        self.assertEqual(out["status"], "succeeded")
        self.assertIn(REDACTED, json.dumps(out))

    def test_redaction_runs_before_the_size_bound(self) -> None:
        # Regression for the repaired seam bug: the old receipt path cut
        # oversized output FIRST and skipped scrubbing entirely, and a cut
        # could split a secret around the boundary. Place a token so its
        # prefix lies inside the old 32768-char cut and its tail outside:
        # only redact-before-bound removes it completely.
        filler = "x" * (MAX_PROJECTION_CHARS - 10)
        text = filler + " sk-" + "H" * 40
        out = project_document(text)
        assert isinstance(out, str)
        self.assertLessEqual(len(out), MAX_PROJECTION_CHARS)
        self.assertNotIn("sk-H", out)
        self.assertNotIn("H" * 40, out)

    def test_oversized_non_json_bounded_after_redaction(self) -> None:
        text = "log line password=" + CANARY + " " + "z" * (MAX_PROJECTION_CHARS * 2)
        out = project_document(text)
        assert isinstance(out, str)
        self.assertLessEqual(len(out), MAX_PROJECTION_CHARS)
        self.assertNotIn(CANARY, out)
        self.assertIn(REDACTED, out)

    def test_oversized_document_falls_back_to_bounded_text(self) -> None:
        raw = json.dumps({"blob": "y" * (MAX_PROJECTION_CHARS * 2), "n": 1})
        out = project_document(raw)
        assert isinstance(out, str)
        self.assertLessEqual(len(out), MAX_PROJECTION_CHARS)

    def test_clean_document_passes_through_intact(self) -> None:
        document = {"status": "succeeded", "exit_code": 0}
        self.assertEqual(project_document(json.dumps(document)), document)


def _windows(token: str, width: int = 24) -> list[str]:
    """Every sliding window of ``token`` — catches partial-prefix leaks."""
    return [token[i : i + width] for i in range(len(token) - width + 1)]


class TestProjectText(unittest.TestCase):
    """The model-history/stream projection: recursive scrub, no re-bound."""

    def test_model_facing_text_redacted(self) -> None:
        out = project_text(f"stdout: {BEARER_TOKEN} password={CANARY}")
        self.assertIn(REDACTED, out)
        self.assertNotIn(CANARY, out)
        self.assertNotIn("G" * 40, out)

    def test_no_rebound_desync_of_truncation_fields(self) -> None:
        # The dispatcher's output budget already bounded this text; the
        # projection must NOT cut again (output_truncated/omitted_bytes
        # describe exactly the upstream bound).
        text = "z" * (MAX_PROJECTION_CHARS + 500)
        self.assertEqual(len(project_text(text)), len(text))

    def test_resolved_values_scrubbed(self) -> None:
        out = project_text(f"value {CANARY} end", [CANARY])
        self.assertNotIn(CANARY, out)
        self.assertIn(REDACTED, out)

    def test_json_escaped_nested_payload_unwrapped_and_redacted(self) -> None:
        # Regression for review finding B1: the escaped inner quotes break
        # key-context matching at the flat layer (password\": "), so the
        # JSON document must be parsed, unwrapped, and re-redacted
        # recursively — while staying unbounded for max_chars=None.
        raw = json.dumps({"encoded": json.dumps({"password": CANARY, "jwt": JWT_TOKEN})})
        out = project_text(raw)
        self.assertNotIn(CANARY, out)
        for window in _windows(CANARY) + _windows(JWT_TOKEN):
            self.assertNotIn(window, out, f"leaked window: {window!r}")
        self.assertIn(REDACTED, out)
        # the projection stays a valid, COMPLETE (untruncated) JSON document
        reparsed = json.loads(out)
        self.assertEqual(json.loads(reparsed["encoded"])["password"], REDACTED)

    def test_explicit_max_chars_bounds_after_redaction(self) -> None:
        # Secret inside the kept head: the cut runs after redaction and
        # never reintroduces a partial secret.
        text = f"password={CANARY}\n" + "y" * (MAX_PROJECTION_CHARS * 2)
        out = project_text(text, max_chars=1024)
        self.assertEqual(len(out), 1024)
        self.assertNotIn(CANARY, out)
        self.assertIn(REDACTED, out)

    def test_s1_canary_document_zero_secret_windows(self) -> None:
        # The runtime S1 composition (nested, JSON-escaped, oversized,
        # resolved values passed as ``secrets``) must leave zero 24-char
        # windows of any secret-bearing fixture token in the persisted
        # model-history projection.
        canary = "fxt-s1-canary-0123456789abcdefghijklmnopqrstuvwxyz"
        sk = "sk-" + "A" * 4200
        bearer = "Bearer " + "G" * 4200
        basic = "Authorization: Basic " + "cXdlcnR5" + "a" * 4200
        pem = "-----BEGIN PRIVATE KEY-----\n" + "C" * 2200 + "\n-----END PRIVATE KEY-----"
        jwt = "eyJ" + "D" * 1400 + "." + "E" * 1400 + "." + "F" * 1500
        userinfo = f"postgres://deploy:{canary}@db.internal:5432/mai"
        encoded_inner = json.dumps({"password": canary, "api_key": canary, "jwt": jwt})
        document = json.dumps(
            {
                "marker": f"fixture-read marker=s1 {canary}",
                "credentials": {
                    "api_key": sk,
                    "bearer": bearer,
                    "basic": basic,
                    "pem": pem,
                    "jwt": jwt,
                    "userinfo": userinfo,
                    "encoded": encoded_inner,
                },
                "filler": "z" * (MAX_PROJECTION_CHARS * 2),
                "n": 1,
            },
            sort_keys=True,
        )
        out = project_text(document, [canary])
        # no re-bound: the clean 64 KiB filler survives the projection
        self.assertGreater(len(out), MAX_PROJECTION_CHARS * 2)
        for token in (canary, sk, bearer, basic, pem, jwt, userinfo):
            for window in _windows(token):
                self.assertNotIn(window, out, f"leaked window: {window!r}")
        self.assertIn(REDACTED, out)


class TestBoundText(unittest.TestCase):
    """The explicit, post-redaction size bound."""

    def test_bound_applies_only_when_exceeded(self) -> None:
        self.assertEqual(bound_text("short"), "short")
        cut = bound_text("y" * (MAX_PROJECTION_CHARS + 1))
        self.assertEqual(len(cut), MAX_PROJECTION_CHARS)

    def test_bound_of_redacted_text_never_splits_marker(self) -> None:
        text = "z" * (MAX_PROJECTION_CHARS - 4) + f" password={CANARY}"
        cut = bound_text(redact_text(text))
        self.assertEqual(len(cut), MAX_PROJECTION_CHARS)
        self.assertNotIn(CANARY, cut)


class TestRedactValues(unittest.TestCase):
    """Layer 1: resolved-credential value forms only."""

    def test_raw_and_escaped_forms_replaced_longest_first(self) -> None:
        secret = 'a"b\\c' + CANARY
        raw = f'v={secret}'
        escaped = json.dumps(secret, ensure_ascii=True)[1:-1]
        for text in (raw, f'{{"v": "{escaped}"}}'):
            out = redact_values(text, [secret])
            self.assertNotIn(secret, out)
            self.assertIn(REDACTED, out)

    def test_no_secrets_is_identity(self) -> None:
        text = "nothing to scrub"
        self.assertEqual(redact_values(text, []), text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
