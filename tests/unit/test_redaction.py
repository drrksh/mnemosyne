"""Secret redaction: every known credential format is removed, ordinary
developer prose and code are left intact."""
from __future__ import annotations

import pytest

from mnemosyne.core.redaction import redact, remaining_substance

SECRETS = {
    "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
    "github_token": "ghp_" + "a1B2c3D4e5" * 4,
    "gitlab_token": "glpat-" + "x1Y2z3" * 4,
    "slack_token": "xoxb-1234567890-abcdefghij",
    "stripe_key": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "anthropic_key": "sk-ant-api03-" + "abcDEF123_" * 3,
    "openai_key": "sk-proj-" + "abcdefghijklmnopqrstuvwx",
    "google_api_key": "AIza" + "SyA1234567890abcdefghijklmnopqrstuv",
    "mnemosyne_key": "mn_0123456789ab_" + "Z" * 30,
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
}


@pytest.mark.parametrize("kind,secret", SECRETS.items())
def test_known_token_formats_are_redacted(kind, secret):
    result = redact(f"here it is: {secret} ok")
    assert secret not in result.text
    assert kind in result.findings


def test_private_key_blocks_are_redacted():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    result = redact(f"key:\n{pem}\nend")
    assert "MIIEow" not in result.text and "private_key" in result.findings


def test_credentials_in_urls_keep_host_and_user():
    result = redact("DATABASE_URL=postgres://admin:s3cr3tP4ss@db.internal:5432/app")
    assert "s3cr3tP4ss" not in result.text
    assert "postgres://admin:" in result.text and "@db.internal:5432/app" in result.text


@pytest.mark.parametrize(
    "text",
    [
        'password = "hunter2hunter2"',
        "export API_KEY=AbCdEf123456",
        "client_secret: 9f8e7d6c5b4a3210",
        "--token Abc12345xyz9",
    ],
)
def test_credential_assignments_are_redacted(text):
    result = redact(text)
    assert "credential" in result.findings


@pytest.mark.parametrize(
    "text",
    [
        "the token expired yesterday so I logged in again",
        "def connect(api_key: Optional[str] = None) -> Client:",
        "password reset flow is handled by the auth service",
        "token = os.environ['API_TOKEN']",
        "secret: ${{ secrets.DEPLOY_KEY }}",
        "fixed in commit 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b",
        "Use pnpm and keep line length at 100",
        "see /usr/local/lib/python3.11/site-packages/package/module.py",
    ],
)
def test_ordinary_prose_and_code_are_untouched(text):
    result = redact(text)
    assert result.text == text, result.findings


def test_remaining_substance():
    assert remaining_substance(redact("key: AKIAIOSFODNN7EXAMPLE").text) < 3
    assert remaining_substance(redact("CI deploys with token AKIAIOSFODNN7EXAMPLE nightly").text) >= 3
