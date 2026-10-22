import base64
import hashlib
import hmac
import json
from unittest.mock import patch

import mongomock

from app import create_app


class ProviderResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class TestAuthSecurity:
    def setup_method(self):
        self.mongo = mongomock.MongoClient()
        self.app = create_app(
            self.mongo,
            {
                "TESTING": True,
                "APP_ENV": "test",
                "SECRET_KEY": "test-secret-key-that-is-at-least-32-characters",
                "APP_BASE_URL": "https://soltor.example.test",
                "APP_TERMS_URL": "https://soltor.example.test/terms",
                "APP_PRIVACY_URL": "https://soltor.example.test/privacy",
                "MONGODB_DATABASE": "test-soltor",
                "RATELIMIT_STORAGE_URI": "memory://",
                "RATELIMIT_ENABLED": False,
                "RESEND_API_KEY": "test-resend-key",
                "RESEND_FROM_EMAIL": "Soltor <accounts@example.test>",
            },
        )
        self.client = self.app.test_client()
        self.database = self.mongo["test-soltor"]
        self.csrf = self.client.get("/api/auth/csrf").get_json()["csrf_token"]
        self.provider_patch = patch(
            "app.urllib.request.urlopen",
            return_value=ProviderResponse({"id": "provider-message"}),
        )
        self.provider_patch.start()
        self.add_cleanup = self.provider_patch.stop

    def teardown_method(self):
        if hasattr(self, "add_cleanup"):
            self.add_cleanup()
        self.mongo.close()

    def post_json(self, client, path, payload, csrf=None, origin="http://localhost"):
        token = csrf
        if token is None:
            token = client.get("/api/auth/csrf").get_json()["csrf_token"]
        return client.post(
            path,
            json=payload,
            headers={"X-CSRFToken": token, "Origin": origin},
        )

    def create_verified_user(self, client, email, name="Jordan Davis"):
        with patch("app.secrets.randbelow", return_value=123456):
            response = self.post_json(client, "/api/auth/register", {
                "name": name,
                "email": email,
                "password": "correct-horse-battery-staple",
                "accepted_terms": True,
            })
        assert response.status_code == 202, response.get_json()
        challenge_id = response.get_json()["challenge_id"]
        verified = self.post_json(client, "/api/auth/verify", {
            "challenge_id": challenge_id,
            "code": "123456",
        })
        assert verified.status_code == 200, verified.get_json()
        return verified.get_json()["user"]["name"]

    def test_signup_login_logout_and_user_data_are_isolated(self):
        assert self.client.get("/api/dashboard").status_code == 401
        self.create_verified_user(self.client, "jordan@example.test")
        user_one = self.database.users.find_one({"email": "jordan@example.test"})
        self.database.accounts.insert_one({
            "id": "private-account-1",
            "owner_id": user_one["id"],
            "currency": "NGN",
            "name": "Personal",
            "balance_minor": 0,
            "usd_equivalent_minor": 0,
            "masked_number": "•••• 0001",
        })

        second_client = self.app.test_client()
        self.create_verified_user(second_client, "alex@example.test", "Alex Morgan")
        user_two = self.database.users.find_one({"email": "alex@example.test"})
        self.database.accounts.insert_one({
            "id": "private-account-2",
            "owner_id": user_two["id"],
            "currency": "NGN",
            "name": "Personal",
            "balance_minor": 0,
            "usd_equivalent_minor": 0,
            "masked_number": "•••• 0002",
        })

        dashboard_one = self.client.get("/api/dashboard").get_json()
        dashboard_two = second_client.get("/api/dashboard").get_json()
        assert [row["id"] for row in dashboard_one["accounts"]] == ["private-account-1"]
        assert [row["id"] for row in dashboard_two["accounts"]] == ["private-account-2"]

        logout = self.post_json(self.client, "/api/auth/logout", {})
        assert logout.status_code == 200
        assert self.client.get("/api/auth/me").status_code == 401
        assert self.database.auth_sessions.count_documents({"revoked_at": None}) == 1

    def test_csrf_origin_and_sensitive_static_files_are_protected(self):
        terms = self.client.get("/terms").data
        privacy = self.client.get("/privacy").data
        assert b"Terms of Service" in terms
        assert b"Privacy Notice" in privacy
        assert b"DRAFT FOR LEGAL REVIEW" not in terms + privacy

        rejected_origin = self.post_json(
            self.client,
            "/api/auth/login",
            {"identifier": "nobody@example.test", "password": "incorrect-password"},
            origin="https://attacker.example",
        )
        assert rejected_origin.status_code == 403

        no_csrf = self.client.post(
            "/api/auth/login",
            json={"identifier": "nobody@example.test", "password": "incorrect-password"},
        )
        assert no_csrf.status_code == 400
        assert self.client.get("/.env").status_code == 404
        assert self.client.get("/app.py").status_code == 404
        assert self.client.get("/index.html").status_code == 404
        assert self.client.get("/styles.css").status_code == 200
        assert self.client.get("/login").headers["X-Frame-Options"] == "DENY"

    def test_password_reset_revokes_existing_sessions_and_requires_one_time_code(self):
        with patch("app.secrets.randbelow", return_value=123456):
            self.create_verified_user(self.client, "jordan@example.test")
            user = self.database.users.find_one({"email": "jordan@example.test"})
            reset_response = self.post_json(
                self.client,
                "/api/auth/forgot-password",
                {"identifier": "jordan@example.test"},
            )
            challenge_id = reset_response.get_json()["challenge_id"]
            verified = self.post_json(self.client, "/api/auth/verify", {
                "challenge_id": challenge_id,
                "code": "123456",
            })
        reset_token = verified.get_json()["reset_token"]
        assert verified.get_json()["status"] == "password_reset_required"
        assert self.database.auth_sessions.count_documents({"user_id": user["id"], "revoked_at": None}) == 1

        updated = self.post_json(self.client, "/api/auth/reset-password", {
            "reset_token": reset_token,
            "password": "a-brand-new-safe-password",
        })
        assert updated.status_code == 200
        assert self.client.get("/api/auth/me").status_code == 401
        assert self.database.auth_sessions.count_documents({"user_id": user["id"], "revoked_at": None}) == 0

        used_again = self.post_json(self.client, "/api/auth/reset-password", {
            "reset_token": reset_token,
            "password": "another-new-safe-password",
        })
        assert used_again.status_code == 400
        with patch("app.secrets.randbelow", return_value=123456):
            login = self.post_json(self.client, "/api/auth/login", {
                "identifier": "jordan@example.test",
                "password": "a-brand-new-safe-password",
            })
        assert login.status_code == 202

    def test_smile_id_verdict_comes_from_signed_webhook_and_does_not_store_id_fields(self):
        self.create_verified_user(self.client, "jordan@example.test")
        user = self.database.users.find_one({"email": "jordan@example.test"})
        self.app.config.update({
            "SMILE_ID_PARTNER_ID": "12345",
            "SMILE_ID_API_KEY": "smile-test-api-key",
            "SMILE_ID_ENVIRONMENT": "sandbox",
            "SMILE_ID_PARTNER_NAME": "Soltor",
            "SMILE_ID_LOGO_URL": "https://soltor.example.test/logo.png",
        })
        with patch(
            "app.urllib.request.urlopen",
            return_value=ProviderResponse({"token": "short-lived-test-token"}),
        ):
            started = self.post_json(self.client, "/api/kyc/session", {"consent": True})
        assert started.status_code == 201, started.get_json()
        assert started.get_json()["token"] == "short-lived-test-token"
        job = self.database.kyc_jobs.find_one({"owner_id": user["id"]})

        timestamp = "2026-10-22T08:00:00Z"
        signature = base64.b64encode(hmac.new(
            b"smile-test-api-key",
            f"{timestamp}12345sid_request".encode(),
            hashlib.sha256,
        ).digest()).decode()
        webhook = {
            "status": "clear",
            "product": "biometric_kyc",
            "partner_params": {
                "job_id": "smile-job-1",
                "user_id": job["smile_user_id"],
            },
            "id_fields": {"id_number": "sensitive-test-identifier"},
        }
        headers = {
            "Response-Signature": signature,
            "Response-Timestamp": timestamp,
        }
        accepted = self.client.post("/api/kyc/smile-webhook", json=webhook, headers=headers)
        assert accepted.status_code == 200, accepted.get_json()
        assert self.database.users.find_one({"id": user["id"]})["kyc_status"] == "verified"
        assert self.database.kyc_jobs.find_one({"smile_user_id": job["smile_user_id"]})["status"] == "verified"
        assert "id_fields" not in self.database.kyc_jobs.find_one(
            {"smile_user_id": job["smile_user_id"]}
        )
        assert self.client.get("/api/kyc/status").get_json()["status"] == "verified"

        bad_signature = self.client.post(
            "/api/kyc/smile-webhook",
            json=webhook,
            headers={"Response-Signature": "invalid", "Response-Timestamp": timestamp},
        )
        assert bad_signature.status_code == 401
