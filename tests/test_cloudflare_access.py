"""Pruebas unitarias de la frontera entre Cloudflare Access y Flask."""

import base64
import json
import time
import unittest
from unittest.mock import MagicMock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from flask import Flask, g

from gestion_abonos_app import _build_content_security_policy
from gestion_abonos_app.services.cloudflare_access import (
    AccessConfigurationError,
    AccessIdentity,
    AccessTokenError,
    CloudflareAccessValidator,
    register_cloudflare_access,
)


TEAM_DOMAIN = "equipo-pruebas.cloudflareaccess.com"
AUDIENCE = "a" * 64
KID = "clave-pruebas"


def _segment(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class CloudflareAccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def setUp(self):
        self.now = int(time.time())
        self.validator = CloudflareAccessValidator(TEAM_DOMAIN, AUDIENCE)
        self.validator._keys = {KID: self.public_key}
        self.validator._cache_expires_at = float("inf")

    def token(self, **changes) -> str:
        payload = {
            "aud": [AUDIENCE],
            "email": "Gestor@Example.com",
            "exp": self.now + 600,
            "iat": self.now,
            "nbf": self.now,
            "iss": f"https://{TEAM_DOMAIN}",
            "sub": "usuario-cloudflare",
            "type": "app",
        }
        payload.update(changes)
        encoded_header = _segment({"alg": "RS256", "kid": KID, "typ": "JWT"})
        encoded_payload = _segment(payload)
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        signature = self.private_key.sign(
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return f"{encoded_header}.{encoded_payload}.{_segment(signature)}"

    def test_accepts_valid_identity(self):
        identity = self.validator.validate(self.token(), now_ts=self.now)
        self.assertEqual(identity.subject, "usuario-cloudflare")
        self.assertEqual(identity.email, "gestor@example.com")

    def test_csp_defines_directives_without_default_src_fallback(self):
        policy = _build_content_security_policy()
        self.assertIn("form-action 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)

    def test_rejects_wrong_claims_and_invalid_signature(self):
        invalid_tokens = (
            self.token(aud=["b" * 64]),
            self.token(iss="https://otro.cloudflareaccess.com"),
            self.token(type="org"),
            self.token(exp=self.now - 60),
            self.token(nbf=self.now + 60),
            self.token(iat=self.now - 10, exp=self.now + (9 * 60 * 60)),
            self.token(email=""),
        )
        for token in invalid_tokens:
            with self.subTest(token=token[:30]), self.assertRaises(AccessTokenError):
                self.validator.validate(token, now_ts=self.now)

        token = self.token()
        manipulated = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
        with self.assertRaises(AccessTokenError):
            self.validator.validate(manipulated, now_ts=self.now)

    def test_fetches_bounded_jwks_without_redirects_or_environment_proxies(self):
        numbers = self.public_key.public_numbers()
        jwk = {
            "keys": [{
                "kty": "RSA",
                "kid": KID,
                "n": _segment(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": _segment(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }]
        }
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        response.iter_content.return_value = [json.dumps(jwk).encode("utf-8")]
        validator = CloudflareAccessValidator(TEAM_DOMAIN, AUDIENCE)
        validator._session.get = MagicMock(return_value=response)

        validator.validate(self.token(), now_ts=self.now)

        self.assertFalse(validator._session.trust_env)
        kwargs = validator._session.get.call_args.kwargs
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["verify"])
        self.assertEqual(validator._session.get.call_args.args[0], validator.jwks_url)

    def test_configuration_is_fail_closed(self):
        for team_domain, audience in (
            ("", AUDIENCE),
            ("https://equipo.cloudflareaccess.com", AUDIENCE),
            ("equipo.example.com", AUDIENCE),
            (TEAM_DOMAIN, "short"),
        ):
            with self.subTest(team_domain=team_domain, audience=audience):
                with self.assertRaises(AccessConfigurationError):
                    CloudflareAccessValidator(team_domain, audience)

    def test_unknown_kid_does_not_force_repeated_jwks_downloads(self):
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        response.iter_content.return_value = [json.dumps({"keys": [{
            "kty": "RSA",
            "kid": KID,
            "n": _segment(self.public_key.public_numbers().n.to_bytes(256, "big")),
            "e": _segment(b"\x01\x00\x01"),
        }]}).encode("utf-8")]
        validator = CloudflareAccessValidator(TEAM_DOMAIN, AUDIENCE)
        validator._session.get = MagicMock(return_value=response)

        encoded_header = _segment({"alg": "RS256", "kid": "kid-inexistente"})
        parts = self.token().split(".")
        signing_input = f"{encoded_header}.{parts[1]}".encode("ascii")
        signature = self.private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        unknown_token = f"{encoded_header}.{parts[1]}.{_segment(signature)}"

        for _ in range(2):
            with self.assertRaises(AccessTokenError):
                validator.validate(unknown_token, now_ts=self.now)
        self.assertEqual(validator._session.get.call_count, 1)

    def test_flask_gate_blocks_before_the_route_and_exempts_healthcheck(self):
        app = Flask(__name__)

        @app.get("/healthz")
        def healthz():
            return "ok"

        @app.get("/private")
        def private():
            return g.cloudflare_access_identity.email

        mocked_validator = MagicMock()
        mocked_validator.validate.side_effect = AccessTokenError("firma_invalida")
        register_cloudflare_access(app, mocked_validator)
        client = app.test_client()

        self.assertEqual(client.get("/healthz").status_code, 200)
        denied = client.get("/private")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.headers["Cache-Control"], "no-store")

        mocked_validator.validate.side_effect = None
        mocked_validator.validate.return_value = AccessIdentity("sub", "gestor@example.com")
        allowed = client.get(
            "/private",
            headers={"Cf-Access-Jwt-Assertion": "token"},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.text, "gestor@example.com")


if __name__ == "__main__":
    unittest.main()
