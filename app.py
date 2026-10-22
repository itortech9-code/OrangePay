import hashlib
import base64
import hmac
import json
import os
import re
import secrets
import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, request, send_from_directory, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
from werkzeug.security import check_password_hash, generate_password_hash


load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_ASSETS = {"styles.css", "script.js", "api.js", "auth.js", "vessel.svg", "legal.css"}
DATABASE_NAME = os.getenv("MONGODB_DATABASE", "soltor_fintech")
INITIAL_ACCOUNTS = [
    {
        "id": "account-usd",
        "currency": "USD",
        "name": "US Dollar",
        "masked_number": "•••• 4829",
        "balance_minor": 1245000,
        "usd_equivalent_minor": 1245000,
    },
    {
        "id": "account-eur",
        "currency": "EUR",
        "name": "Euro",
        "masked_number": "•••• 1934",
        "balance_minor": 820000,
        "usd_equivalent_minor": 889280,
    },
    {
        "id": "account-gbp",
        "currency": "GBP",
        "name": "British Pound",
        "masked_number": "•••• 7401",
        "balance_minor": 265000,
        "usd_equivalent_minor": 333770,
    },
]
INITIAL_TRANSACTIONS = [
    {
        "id": "seed-stripe-payout",
        "kind": "income",
        "description": "Sample customer payment",
        "currency": "USD",
        "amount_minor": 245000,
        "account_currency": "USD",
        "status": "Demo",
        "minutes_ago": 15,
    },
    {
        "id": "seed-alex-transfer",
        "kind": "expense",
        "description": "Transfer to Alex Morgan",
        "currency": "USD",
        "amount_minor": 28000,
        "account_currency": "USD",
        "status": "Demo",
        "minutes_ago": 90,
    },
    {
        "id": "seed-coffee",
        "kind": "expense",
        "description": "Blue Bottle Coffee",
        "currency": "USD",
        "amount_minor": 850,
        "account_currency": "USD card",
        "status": "Demo",
        "minutes_ago": 800,
    },
    {
        "id": "seed-northstar",
        "kind": "income",
        "description": "Northstar Studio",
        "currency": "EUR",
        "amount_minor": 120000,
        "account_currency": "EUR",
        "status": "Demo",
        "minutes_ago": 1500,
    },
]
DEMO_RECIPIENTS = ["Alex Morgan", "Mia Chen", "Noah Williams"]
PAYSTACK_API_URL = "https://api.paystack.co"
EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest"
SUPPORTED_CURRENCIES = {
    "AED", "AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "GHS", "INR",
    "JPY", "KES", "NGN", "NZD", "RWF", "SAR", "USD", "ZAR",
}


class DatabaseUnavailable(Exception):
    pass


class ProviderError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def serialize_transaction(transaction):
    happened_at = transaction["created_at"]
    if happened_at.tzinfo is None:
        happened_at = happened_at.replace(tzinfo=timezone.utc)
    return {
        "id": transaction["id"],
        "kind": transaction["kind"],
        "description": transaction["description"],
        "currency": transaction["currency"],
        "amount_minor": transaction["amount_minor"],
        "account_currency": transaction["account_currency"],
        "status": transaction["status"],
        "created_at": happened_at.isoformat(),
    }


def seed_database(database):
    database.users.create_index("email", unique=True, sparse=True)
    database.users.create_index("phone", unique=True, sparse=True)
    database.users.create_index("id", unique=True)
    database.auth_challenges.create_index("id", unique=True)
    database.auth_challenges.create_index("expires_at", expireAfterSeconds=0)
    database.password_reset_tokens.create_index("token_hash", unique=True)
    database.password_reset_tokens.create_index("expires_at", expireAfterSeconds=0)
    database.auth_sessions.create_index("token_hash", unique=True)
    database.auth_sessions.create_index("expires_at", expireAfterSeconds=0)
    database.accounts.create_index([("owner_id", 1), ("id", 1)], unique=True)
    database.transactions.create_index([("owner_id", 1), ("id", 1)], unique=True)
    database.recipients.create_index([("owner_id", 1), ("name", 1)], unique=True)
    database.payment_attempts.create_index("reference", unique=True)
    database.transfers.create_index("reference", unique=True)
    database.bank_links.create_index([("owner_id", 1), ("reference", 1)], unique=True)
    database.bank_links.create_index("account_id", sparse=True)
    database.kyc_jobs.create_index("smile_user_id", unique=True)
    database.kyc_jobs.create_index("job_id", sparse=True, unique=True)
    database.audit_events.create_index([("owner_id", 1), ("created_at", -1)])
    database.audit_events.create_index("event_id", unique=True)


def create_app(client=None, test_config=None):
    app = Flask(__name__, static_folder=None)
    app.config["MONGODB_URI"] = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
    app.config["MONGODB_DATABASE"] = DATABASE_NAME
    app.config["MONGODB_CLIENT"] = client
    app.config["DATABASE_INITIALIZED"] = False
    app.config["APP_ENV"] = os.getenv("APP_ENV", "development").lower()
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "")
    app.config["PAYSTACK_SECRET_KEY"] = os.getenv("PAYSTACK_SECRET_KEY", "")
    app.config["APP_BASE_URL"] = os.getenv("APP_BASE_URL", "").rstrip("/")
    app.config["APP_PRIVACY_URL"] = os.getenv("APP_PRIVACY_URL", "")
    app.config["APP_TERMS_URL"] = os.getenv("APP_TERMS_URL", "")
    app.config["APP_NAME"] = os.getenv("APP_NAME", "Soltor")
    app.config["RATELIMIT_STORAGE_URI"] = os.getenv("REDIS_URL", "memory://")
    app.config["RATELIMIT_ENABLED"] = True
    app.config["MAX_CONTENT_LENGTH"] = 65536
    app.config["SESSION_COOKIE_NAME"] = (
        "__Host-soltor_session" if app.config["APP_ENV"] == "production" else "soltor_session"
    )
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = app.config["APP_ENV"] == "production"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
    app.config["WTF_CSRF_TIME_LIMIT"] = 2 * 60 * 60
    app.config["WTF_CSRF_SSL_STRICT"] = app.config["APP_ENV"] == "production"
    app.config["MONO_SECRET_KEY"] = os.getenv("MONO_SECRET_KEY", "")
    app.config["MONO_WEBHOOK_SECRET"] = os.getenv("MONO_WEBHOOK_SECRET", "")
    app.config["CRISP_WEBSITE_ID"] = os.getenv("CRISP_WEBSITE_ID", "")
    app.config["SMILE_ID_PARTNER_ID"] = os.getenv("SMILE_ID_PARTNER_ID", "")
    app.config["SMILE_ID_API_KEY"] = os.getenv("SMILE_ID_API_KEY", "")
    app.config["SMILE_ID_ENVIRONMENT"] = os.getenv("SMILE_ID_ENVIRONMENT", "sandbox").lower()
    app.config["SMILE_ID_PARTNER_NAME"] = os.getenv("SMILE_ID_PARTNER_NAME", "Soltor")
    app.config["SMILE_ID_LOGO_URL"] = os.getenv("SMILE_ID_LOGO_URL", "")
    app.config["SMILE_ID_THEME_COLOR"] = os.getenv("SMILE_ID_THEME_COLOR", "#51439b")
    app.config["RESEND_API_KEY"] = os.getenv("RESEND_API_KEY", "")
    app.config["RESEND_FROM_EMAIL"] = os.getenv("RESEND_FROM_EMAIL", "")
    app.config["TERMII_API_KEY"] = os.getenv("TERMII_API_KEY", "")
    app.config["TERMII_SENDER_ID"] = os.getenv("TERMII_SENDER_ID", "")
    app.config["TERMII_API_URL"] = os.getenv("TERMII_API_URL", "")
    app.config["EXCHANGE_RATE_CACHE"] = {}
    if test_config:
        app.config.update(test_config)
    if app.config["APP_ENV"] == "production":
        required_settings = {
            "SECRET_KEY": app.config["SECRET_KEY"],
            "MONGODB_URI": app.config["MONGODB_URI"],
            "APP_BASE_URL": app.config["APP_BASE_URL"],
            "APP_PRIVACY_URL": app.config["APP_PRIVACY_URL"],
            "APP_TERMS_URL": app.config["APP_TERMS_URL"],
            "RATELIMIT_STORAGE_URI": app.config["RATELIMIT_STORAGE_URI"],
        }
        missing_settings = [key for key, value in required_settings.items() if not value]
        if missing_settings:
            raise RuntimeError(
                "Production configuration is incomplete: " + ", ".join(missing_settings)
            )
        if len(app.config["SECRET_KEY"]) < 32:
            raise RuntimeError("Production SECRET_KEY must contain at least 32 characters.")
        if not app.config["APP_BASE_URL"].startswith("https://"):
            raise RuntimeError("Production APP_BASE_URL must use HTTPS.")
        if not app.config["APP_PRIVACY_URL"].startswith("https://"):
            raise RuntimeError("Production APP_PRIVACY_URL must use HTTPS.")
        if not app.config["APP_TERMS_URL"].startswith("https://"):
            raise RuntimeError("Production APP_TERMS_URL must use HTTPS.")
        if app.config["RATELIMIT_STORAGE_URI"].startswith("memory://"):
            raise RuntimeError("Production rate limiting requires shared Redis storage.")
    elif not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = secrets.token_hex(32)

    csrf = CSRFProtect()
    csrf.init_app(app)
    limiter = Limiter(
        get_remote_address,
        app=app,
        storage_uri=app.config["RATELIMIT_STORAGE_URI"],
        default_limits=[],
        enabled=app.config["RATELIMIT_ENABLED"],
    )
    app.extensions["soltor_limiter"] = limiter

    @app.before_request
    def check_browser_origin():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        if request.endpoint in {"paystack_webhook", "mono_webhook", "smile_webhook"}:
            return None
        origin = request.headers.get("Origin")
        if not origin:
            return None
        allowed_origins = {
            request.host_url.rstrip("/"),
            app.config["APP_BASE_URL"],
        } - {""}
        if origin.rstrip("/") not in allowed_origins:
            return jsonify({"error": "Request origin is not allowed."}), 403
        return None

    @app.after_request
    def apply_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' https://cdn.usesmileid.com https://client.crisp.chat; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; "
            "connect-src 'self' https://api.withmono.com https://api.smileidentity.com "
            "https://testapi.smileidentity.com https://client.crisp.chat "
            "https://*.crisp.chat https://*.usesmileid.com; frame-src https://*.usesmileid.com "
            "https://usesmileid.com https://*.crisp.chat; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https:",
        )
        if app.config["APP_ENV"] == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    def get_database():
        mongo_client = app.config["MONGODB_CLIENT"]
        try:
            if mongo_client is None:
                mongo_client = MongoClient(
                    app.config["MONGODB_URI"],
                    serverSelectionTimeoutMS=3000,
                    connectTimeoutMS=3000,
                )
                app.config["MONGODB_CLIENT"] = mongo_client
            mongo_client.admin.command("ping")
            database = mongo_client[app.config["MONGODB_DATABASE"]]
            if not app.config["DATABASE_INITIALIZED"]:
                seed_database(database)
                app.config["DATABASE_INITIALIZED"] = True
            return database
        except PyMongoError as error:
            raise DatabaseUnavailable(
                "MongoDB is unavailable. Start MongoDB or set MONGODB_URI to a reachable MongoDB connection string."
            ) from error

    def require_paystack():
        secret_key = app.config["PAYSTACK_SECRET_KEY"]
        if not secret_key.startswith("sk_test_"):
            raise ProviderError(
                "Paystack sandbox is not configured. Set PAYSTACK_SECRET_KEY to a Paystack test secret key."
            )
        return secret_key

    def paystack_request(method, path, payload=None, query=None):
        secret_key = require_paystack()
        url = f"{PAYSTACK_API_URL}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        api_request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {secret_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Soltor-Fintech-Demo/1.0",
            },
        )
        try:
            with urllib.request.urlopen(api_request, timeout=15) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                result = json.loads(error.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                result = {}
            message = result.get("message") if isinstance(result, dict) else None
            raise ProviderError(message or "Paystack could not complete the request.") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError("Could not reach Paystack. Please try again.") from error
        if not isinstance(result, dict) or not result.get("status"):
            message = result.get("message") if isinstance(result, dict) else None
            raise ProviderError(message or "Paystack rejected the request.")
        data = result.get("data")
        if not isinstance(data, (dict, list)):
            raise ProviderError("Paystack returned an invalid response.")
        return data

    def mono_request(method, path, payload=None):
        secret_key = app.config["MONO_SECRET_KEY"]
        if not secret_key or not app.config["MONO_WEBHOOK_SECRET"]:
            raise ProviderError(
                "Mono bank linking is not configured. Add MONO_SECRET_KEY and MONO_WEBHOOK_SECRET to the local .env file."
            )
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        api_request = urllib.request.Request(
            f"https://api.withmono.com{path}",
            data=body,
            method=method,
            headers={
                "mono-sec-key": secret_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Soltor-Fintech-Demo/1.0",
            },
        )
        try:
            with urllib.request.urlopen(api_request, timeout=15) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                response_data = json.loads(error.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                response_data = {}
            message = response_data.get("message") if isinstance(response_data, dict) else None
            raise ProviderError(message or "Mono could not start bank linking.") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError("Could not reach Mono. Please try again.") from error
        if not isinstance(result, dict) or result.get("status") != "successful":
            message = result.get("message") if isinstance(result, dict) else None
            raise ProviderError(message or "Mono rejected the bank-link request.")
        data = result.get("data")
        if not isinstance(data, dict):
            raise ProviderError("Mono returned an invalid bank-link response.")
        return data

    def get_json(url):
        api_request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "Soltor-Fintech-Demo/1.0"},
        )
        try:
            with urllib.request.urlopen(api_request, timeout=10) as response:
                result = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError("Could not retrieve the latest exchange rate. Please try again.") from error
        if not isinstance(result, dict):
            raise ProviderError("The exchange-rate provider returned an invalid response.")
        return result

    def parse_ngn_amount(payload):
        try:
            amount = Decimal(str(payload.get("amount", "")))
            if not amount.is_finite() or amount < Decimal("100") or amount > Decimal("100000000"):
                raise InvalidOperation
            if amount.as_tuple().exponent < -2:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            raise ValueError("Enter an amount between ₦100 and ₦100,000,000 with no more than two decimal places.")
        return int(amount * 100)

    def audit_event(database, event, owner_id=None, details=None):
        database.audit_events.insert_one({
            "event_id": uuid.uuid4().hex,
            "owner_id": owner_id,
            "event": event,
            "details": details or {},
            "created_at": utc_now(),
        })

    def provider_post(url, payload, headers, provider_name):
        body = json.dumps(payload).encode("utf-8")
        api_request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                **headers,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Soltor/1.0",
            },
        )
        try:
            with urllib.request.urlopen(api_request, timeout=15) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            app.logger.warning("%s request failed with HTTP %s", provider_name, error.code)
            raise ProviderError(f"{provider_name} could not complete the request. Please try again.") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            app.logger.warning("%s request could not be completed", provider_name)
            raise ProviderError(f"Could not reach {provider_name}. Please try again.") from error
        if not isinstance(result, dict):
            raise ProviderError(f"{provider_name} returned an invalid response.")
        if result.get("error"):
            app.logger.warning("%s rejected a request", provider_name)
            raise ProviderError(f"{provider_name} could not complete the request. Please try again.")
        return result

    def smile_token(user, consent):
        partner_id = app.config["SMILE_ID_PARTNER_ID"]
        api_key = app.config["SMILE_ID_API_KEY"]
        logo_url = app.config["SMILE_ID_LOGO_URL"]
        if not partner_id or not api_key or not logo_url:
            raise ProviderError(
                "Smile ID is not fully configured. Set partner credentials and a public logo URL in the deployment environment."
            )
        if app.config["SMILE_ID_ENVIRONMENT"] not in {"sandbox", "production"}:
            raise ProviderError("SMILE_ID_ENVIRONMENT must be sandbox or production.")
        parsed_logo = urllib.parse.urlparse(logo_url)
        if parsed_logo.scheme != "https" or not parsed_logo.netloc:
            raise ProviderError("Smile ID requires a public HTTPS logo URL.")
        api_host = (
            "https://api.smileidentity.com"
            if app.config["SMILE_ID_ENVIRONMENT"] == "production"
            else "https://testapi.smileidentity.com"
        )
        smile_user_id = f"soltor_{uuid.uuid4().hex}"
        payload = {
            "country": "NG",
            "callback_url": f"{app.config['APP_BASE_URL']}/api/kyc/smile-webhook",
            "consent": consent,
        }
        if user.get("email"):
            payload["email"] = user["email"]
        if user.get("phone"):
            payload["phone_number"] = user["phone"]
        boundary = f"----Soltor{uuid.uuid4().hex}"
        fields = {
            "user_id": smile_user_id,
            "product": "biometric_kyc",
            "payload": json.dumps(payload),
        }
        parts = []
        for name, value in fields.items():
            parts.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ])
        parts.append(f"--{boundary}--\r\n".encode())
        api_request = urllib.request.Request(
            f"{api_host}/v3/token",
            data=b"".join(parts),
            method="POST",
            headers={
                "smileid-partner-id": partner_id,
                "smileid-api-key": api_key,
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "Soltor/1.0",
            },
        )
        try:
            with urllib.request.urlopen(api_request, timeout=15) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            app.logger.warning("Smile ID token request failed with HTTP %s", error.code)
            raise ProviderError("Smile ID could not start verification. Please try again.") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            app.logger.warning("Smile ID token request could not be completed")
            raise ProviderError("Could not reach Smile ID. Please try again.") from error
        token = result.get("token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise ProviderError("Smile ID returned an invalid verification token.")
        return smile_user_id, token

    def send_auth_code(contact_type, contact, code, challenge_id):
        message = f"Your Soltor verification code is {code}. It expires in 10 minutes."
        if contact_type == "email":
            api_key = app.config["RESEND_API_KEY"]
            sender = app.config["RESEND_FROM_EMAIL"]
            if not api_key or not sender:
                raise ProviderError("Email verification is not configured. Set the Resend credentials in the deployment environment.")
            provider_post(
                "https://api.resend.com/emails",
                {
                    "from": sender,
                    "to": [contact],
                    "subject": "Your Soltor verification code",
                    "text": message,
                },
                {
                    "Authorization": f"Bearer {api_key}",
                    "Idempotency-Key": f"soltor-auth-{challenge_id}",
                },
                "Resend",
            )
            return

        api_key = app.config["TERMII_API_KEY"]
        sender = app.config["TERMII_SENDER_ID"]
        api_url = app.config["TERMII_API_URL"]
        if not api_key or not sender or not api_url:
            raise ProviderError(
                "SMS verification is not configured. Set the Termii API URL, sender ID, and API key in the deployment environment."
            )
        parsed_url = urllib.parse.urlparse(api_url)
        if parsed_url.scheme != "https" or parsed_url.hostname not in {
            "api.ng.termii.com", "api.termii.com"
        }:
            raise ProviderError("Termii API URL must be an HTTPS Termii endpoint.")
        provider_post(
            api_url,
            {
                "api_key": api_key,
                "to": contact,
                "from": sender,
                "sms": message,
                "type": "plain",
                "channel": "generic",
            },
            {},
            "Termii",
        )

    def contact_from_payload(payload):
        email = payload.get("email")
        phone = payload.get("phone")
        if bool(email) == bool(phone):
            raise ValueError("Provide either an email address or a phone number.")
        if email:
            if not isinstance(email, str) or len(email) > 254:
                raise ValueError("Enter a valid email address.")
            normalized = email.strip().casefold()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
                raise ValueError("Enter a valid email address.")
            return "email", normalized
        if not isinstance(phone, str) or not re.fullmatch(r"\+[1-9]\d{7,14}", phone.strip()):
            raise ValueError("Enter a phone number in international E.164 format, including + and country code.")
        return "phone", phone.strip()

    def contact_field(contact_type):
        return "email" if contact_type == "email" else "phone"

    def otp_digest(challenge_id, code):
        return hmac.new(
            app.config["SECRET_KEY"].encode("utf-8"),
            f"{challenge_id}:{code}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def issue_auth_challenge(database, user, purpose):
        challenge_id = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1_000_000):06d}"
        contact_type = user["contact_type"]
        contact = user[contact_field(contact_type)]
        now = utc_now()
        database.auth_challenges.insert_one({
            "id": challenge_id,
            "user_id": user["id"],
            "purpose": purpose,
            "contact_type": contact_type,
            "contact": contact,
            "code_digest": otp_digest(challenge_id, code),
            "attempts": 0,
            "resends": 0,
            "created_at": now,
            "expires_at": now + timedelta(minutes=10),
        })
        try:
            send_auth_code(contact_type, contact, code, challenge_id)
        except ProviderError:
            database.auth_challenges.delete_one({"id": challenge_id})
            raise
        return challenge_id

    def auth_user():
        if getattr(g, "auth_user_loaded", False):
            return getattr(g, "auth_user", None)
        g.auth_user_loaded = True
        user_id = session.get("user_id")
        session_token = session.get("auth_session")
        if not isinstance(user_id, str) or not isinstance(session_token, str):
            g.auth_user = None
            return None
        database = get_database()
        active_session = database.auth_sessions.find_one({
            "token_hash": hashlib.sha256(session_token.encode("utf-8")).hexdigest(),
            "user_id": user_id,
            "revoked_at": None,
            "expires_at": {"$gt": utc_now()},
        })
        if not active_session:
            session.clear()
            g.auth_user = None
            return None
        user = database.users.find_one({"id": user_id, "status": "active"}, {"_id": 0, "password_hash": 0})
        g.auth_user = user
        return user

    def create_user_session(database, user):
        raw_token = secrets.token_urlsafe(48)
        now = utc_now()
        expires_at = now + app.config["PERMANENT_SESSION_LIFETIME"]
        database.auth_sessions.insert_one({
            "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            "user_id": user["id"],
            "created_at": now,
            "expires_at": expires_at,
            "revoked_at": None,
        })
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["auth_session"] = raw_token
        audit_event(database, "auth.session_created", user["id"])

    def require_auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = auth_user()
            if not user:
                return jsonify({"error": "Sign in to continue.", "code": "authentication_required"}), 401
            return view(*args, **kwargs)
        return wrapped

    def handle_provider_error(error):
        status_code = 503 if "is not configured." in str(error) else 502
        return jsonify({"error": str(error)}), status_code

    def record_verified_payment(database, reference, verified):
        attempt = database.payment_attempts.find_one({"reference": reference})
        if not attempt:
            raise ProviderError("Payment reference was not created by this app.")
        if (
            verified.get("status") != "success"
            or verified.get("currency") != "NGN"
            or verified.get("amount") != attempt["amount_minor"]
        ):
            database.payment_attempts.update_one(
                {"reference": reference, "status": "pending"},
                {"$set": {"status": "failed", "updated_at": utc_now()}},
            )
            raise ProviderError("The verified payment did not match the expected amount and currency.")

        payment = database.payment_attempts.find_one_and_update(
            {"reference": reference, "status": {"$ne": "success"}},
            {"$set": {"status": "success", "updated_at": utc_now()}},
            return_document=True,
        )
        if payment or attempt["status"] == "success":
            database.transactions.update_one(
                {"id": f"paystack-{reference}"},
                {
                    "$setOnInsert": {
                        "id": f"paystack-{reference}",
                        "kind": "income",
                        "description": f"Payment from {attempt['email']}",
                        "currency": "NGN",
                        "amount_minor": attempt["amount_minor"],
                        "account_currency": "Paystack merchant account",
                        "status": "Paid",
                        "owner_id": attempt["owner_id"],
                        "created_at": utc_now(),
                    }
                },
                upsert=True,
            )
        return database.payment_attempts.find_one({"reference": reference}, {"_id": 0})

    def record_transfer_status(database, transfer_data, event_status=None):
        reference = transfer_data.get("reference")
        if not isinstance(reference, str):
            raise ProviderError("Transfer event did not include a reference.")
        transfer = database.transfers.find_one({"reference": reference})
        if not transfer:
            raise ProviderError("Transfer reference was not created by this app.")
        if (
            transfer_data.get("amount") is not None
            and transfer_data["amount"] != transfer["amount_minor"]
        ) or (
            transfer_data.get("currency") is not None
            and transfer_data["currency"] != transfer["currency"]
        ):
            raise ProviderError("The transfer event did not match the expected amount and currency.")

        status = event_status or transfer_data.get("status", "pending")
        display_status = {
            "success": "Paid",
            "failed": "Failed",
            "reversed": "Reversed",
            "otp": "Requires OTP",
        }.get(status, "Pending")
        updates = {"status": status, "updated_at": utc_now()}
        if transfer_data.get("transfer_code"):
            updates["transfer_code"] = transfer_data["transfer_code"]
        database.transfers.update_one({"reference": reference}, {"$set": updates})
        database.transactions.update_one(
            {"id": f"paystack-transfer-{reference}"},
            {
                "$setOnInsert": {
                    "id": f"paystack-transfer-{reference}",
                    "kind": "expense",
                    "description": f"Transfer to {transfer['recipient_name']}",
                    "currency": transfer["currency"],
                    "amount_minor": transfer["amount_minor"],
                    "account_currency": "Paystack merchant balance",
                    "owner_id": transfer["owner_id"],
                    "created_at": utc_now(),
                },
                "$set": {"status": display_status},
            },
            upsert=True,
        )
        return database.transfers.find_one({"reference": reference}, {"_id": 0})

    @app.errorhandler(DatabaseUnavailable)
    def handle_database_unavailable(error):
        return jsonify({"error": str(error)}), 503

    @app.errorhandler(PyMongoError)
    def handle_database_error(error):
        app.logger.exception("MongoDB request failed")
        return jsonify({"error": "The database request failed. Please try again."}), 503

    @app.errorhandler(ProviderError)
    def handle_provider_failure(error):
        return handle_provider_error(error)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Your security token expired. Refresh the page and try again."}), 400
        return "The request could not be verified. Refresh the page and try again.", 400

    @app.get("/api/auth/csrf")
    def auth_csrf_token():
        return jsonify({"csrf_token": generate_csrf()})

    @app.post("/api/auth/register")
    @limiter.limit("5 per hour")
    def register():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send a JSON object with your name, contact, password, and consent."}), 400
        name = payload.get("name")
        password = payload.get("password")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            return jsonify({"error": "Enter your name (up to 100 characters)."}), 400
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            return jsonify({"error": "Use a password between 12 and 128 characters."}), 400
        if payload.get("accepted_terms") is not True:
            return jsonify({"error": "Accept the terms and privacy notice to create an account."}), 400
        if not app.config["APP_TERMS_URL"].startswith("https://") or not app.config["APP_PRIVACY_URL"].startswith("https://"):
            raise ProviderError("Account registration is unavailable until public HTTPS terms and privacy notices are configured.")
        try:
            contact_type, contact = contact_from_payload(payload)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        database = get_database()
        user_id = f"user_{uuid.uuid4().hex}"
        now = utc_now()
        user = {
            "id": user_id,
            "name": name.strip(),
            "contact_type": contact_type,
            contact_field(contact_type): contact,
            "password_hash": generate_password_hash(password, method="scrypt"),
            "status": "pending_verification",
            "created_at": now,
            "updated_at": now,
            "kyc_status": "not_started",
            "terms_accepted_at": now,
            "terms_version": "2026-10-22",
        }
        try:
            database.users.insert_one(user)
        except DuplicateKeyError:
            return jsonify({"error": "An account with that contact already exists. Sign in or use account recovery."}), 409

        try:
            challenge_id = issue_auth_challenge(database, user, "register")
        except ProviderError:
            database.users.delete_one({"id": user_id, "status": "pending_verification"})
            raise
        audit_event(database, "auth.registration_started", user_id, {"contact_type": contact_type})
        return jsonify({
            "challenge_id": challenge_id,
            "message": f"Enter the verification code sent to your {contact_type}.",
        }), 202

    @app.post("/api/auth/login")
    @limiter.limit("5 per minute")
    def login():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send your email or phone number and password."}), 400
        identifier = payload.get("identifier")
        password = payload.get("password")
        if not isinstance(identifier, str) or not isinstance(password, str) or len(password) > 128:
            return jsonify({"error": "Enter your email or phone number and password."}), 400
        identifier = identifier.strip()
        if "@" in identifier:
            identifier = identifier.casefold()
            field = "email"
        else:
            field = "phone"
        database = get_database()
        user = database.users.find_one({field: identifier})
        valid_password = check_password_hash(
            user["password_hash"] if user else generate_password_hash("unavailable-account-password", method="scrypt"),
            password,
        )
        if not user or not valid_password:
            return jsonify({"error": "The sign-in details are not correct."}), 401
        if user["status"] != "active":
            return jsonify({"error": "Verify your contact address before signing in."}), 403
        challenge_id = issue_auth_challenge(database, user, "login")
        audit_event(database, "auth.login_challenge_created", user["id"], {"contact_type": user["contact_type"]})
        return jsonify({
            "challenge_id": challenge_id,
            "message": f"Enter the sign-in code sent to your verified {user['contact_type']}.",
        }), 202

    @app.post("/api/auth/verify")
    @limiter.limit("10 per minute")
    def verify_auth_code():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send a verification challenge and code."}), 400
        challenge_id = payload.get("challenge_id")
        code = payload.get("code")
        if (
            not isinstance(challenge_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", challenge_id)
            or not isinstance(code, str)
            or not re.fullmatch(r"\d{6}", code)
        ):
            return jsonify({"error": "Enter the six-digit verification code."}), 400
        database = get_database()
        now = utc_now()
        challenge_query = {
            "id": challenge_id,
            "expires_at": {"$gt": now},
            "attempts": {"$lt": 5},
            "consumed_at": {"$exists": False},
        }
        challenge = database.auth_challenges.find_one(challenge_query)
        if not challenge:
            return jsonify({"error": "The verification code is invalid or expired. Request a new code."}), 400
        expected = challenge["code_digest"]
        supplied = otp_digest(challenge_id, code)
        if not hmac.compare_digest(expected, supplied):
            database.auth_challenges.update_one(
                {**challenge_query, "code_digest": expected},
                {"$inc": {"attempts": 1}},
            )
            return jsonify({"error": "The verification code is invalid or expired. Request a new code."}), 400
        consumed = database.auth_challenges.find_one_and_update(
            {**challenge_query, "code_digest": expected},
            {"$set": {"consumed_at": now}},
            return_document=True,
        )
        if not consumed:
            return jsonify({"error": "The verification code was already used. Request a new code."}), 400
        user = database.users.find_one({"id": challenge["user_id"]})
        if not user:
            return jsonify({"error": "The account is unavailable. Please register again."}), 404
        if challenge["purpose"] == "register":
            database.users.update_one(
                {"id": user["id"], "status": "pending_verification"},
                {"$set": {"status": "active", "contact_verified_at": now, "updated_at": now}},
            )
            audit_event(database, "auth.contact_verified", user["id"], {"contact_type": user["contact_type"]})
            user["status"] = "active"
        if challenge["purpose"] == "password_reset":
            reset_token = secrets.token_urlsafe(48)
            database.password_reset_tokens.insert_one({
                "token_hash": hashlib.sha256(reset_token.encode("utf-8")).hexdigest(),
                "user_id": user["id"],
                "created_at": now,
                "expires_at": now + timedelta(minutes=15),
                "consumed_at": None,
            })
            audit_event(database, "auth.password_reset_code_verified", user["id"])
            return jsonify({"status": "password_reset_required", "reset_token": reset_token})
        if user.get("status") != "active":
            return jsonify({"error": "Verify your contact address before signing in."}), 403
        create_user_session(database, user)
        return jsonify({"status": "authenticated", "user": {
            "name": user["name"],
            "contact_type": user["contact_type"],
            "email": user.get("email"),
            "phone": user.get("phone"),
            "kyc_status": user.get("kyc_status", "not_started"),
        }})

    @app.post("/api/auth/resend")
    @limiter.limit("3 per hour")
    def resend_auth_code():
        payload = request.get_json(silent=True)
        challenge_id = payload.get("challenge_id") if isinstance(payload, dict) else None
        if not isinstance(challenge_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", challenge_id):
            return jsonify({"error": "Request a valid verification code first."}), 400
        database = get_database()
        now = utc_now()
        challenge = database.auth_challenges.find_one({
            "id": challenge_id,
            "expires_at": {"$gt": now},
            "consumed_at": {"$exists": False},
            "resends": {"$lt": 3},
        })
        if not challenge:
            return jsonify({"error": "This verification request expired. Start again."}), 400
        code = f"{secrets.randbelow(1_000_000):06d}"
        new_digest = otp_digest(challenge_id, code)
        updated = database.auth_challenges.find_one_and_update(
            {"_id": challenge["_id"], "consumed_at": {"$exists": False}, "resends": {"$lt": 3}},
            {
                "$set": {
                    "code_digest": new_digest,
                    "attempts": 0,
                    "expires_at": now + timedelta(minutes=10),
                },
                "$inc": {"resends": 1},
            },
            return_document=True,
        )
        if not updated:
            return jsonify({"error": "This verification request expired. Start again."}), 400
        try:
            send_auth_code(
                updated["contact_type"],
                updated["contact"],
                code,
                f"{challenge_id}-{updated['resends']}",
            )
        except ProviderError:
            database.auth_challenges.update_one(
                {"_id": updated["_id"], "code_digest": new_digest},
                {"$set": {"expires_at": now}},
            )
            raise
        return jsonify({"message": f"A new code was sent to your {updated['contact_type']}."})

    @app.post("/api/auth/forgot-password")
    @limiter.limit("3 per hour")
    def forgot_password():
        payload = request.get_json(silent=True)
        identifier = payload.get("identifier") if isinstance(payload, dict) else None
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 254:
            return jsonify({"error": "Enter the email address or phone number for your account."}), 400
        identifier = identifier.strip()
        field = "email" if "@" in identifier else "phone"
        identifier = identifier.casefold() if field == "email" else identifier
        database = get_database()
        user = database.users.find_one({field: identifier, "status": "active"})
        response = {"message": "If an active account matches that contact, a recovery code has been sent."}
        if not user:
            return jsonify(response), 202
        challenge_id = issue_auth_challenge(database, user, "password_reset")
        audit_event(database, "auth.password_reset_started", user["id"], {
            "contact_type": user["contact_type"],
        })
        response["challenge_id"] = challenge_id
        return jsonify(response), 202

    @app.post("/api/auth/reset-password")
    @limiter.limit("5 per hour")
    def reset_password():
        payload = request.get_json(silent=True)
        reset_token = payload.get("reset_token") if isinstance(payload, dict) else None
        password = payload.get("password") if isinstance(payload, dict) else None
        if not isinstance(reset_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{50,80}", reset_token):
            return jsonify({"error": "Request a new password reset code and try again."}), 400
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            return jsonify({"error": "Use a password between 12 and 128 characters."}), 400
        database = get_database()
        now = utc_now()
        reset_record = database.password_reset_tokens.find_one_and_update(
            {
                "token_hash": hashlib.sha256(reset_token.encode("utf-8")).hexdigest(),
                "expires_at": {"$gt": now},
                "consumed_at": None,
            },
            {"$set": {"consumed_at": now}},
            return_document=True,
        )
        if not reset_record:
            return jsonify({"error": "The password reset request expired or was already used."}), 400
        database.users.update_one(
            {"id": reset_record["user_id"], "status": "active"},
            {"$set": {
                "password_hash": generate_password_hash(password, method="scrypt"),
                "updated_at": now,
            }},
        )
        database.auth_sessions.update_many(
            {"user_id": reset_record["user_id"], "revoked_at": None},
            {"$set": {"revoked_at": now}},
        )
        audit_event(database, "auth.password_reset_completed", reset_record["user_id"])
        return jsonify({"status": "password_updated"})

    @app.get("/api/auth/me")
    @require_auth
    def authenticated_user():
        user = g.auth_user
        return jsonify({"user": {
            "name": user["name"],
            "contact_type": user["contact_type"],
            "email": user.get("email"),
            "phone": user.get("phone"),
            "kyc_status": user.get("kyc_status", "not_started"),
        }})

    @app.get("/api/public/config")
    def public_config():
        return jsonify({
            "terms_url": app.config["APP_TERMS_URL"],
            "privacy_url": app.config["APP_PRIVACY_URL"],
        })

    @app.post("/api/auth/logout")
    @require_auth
    def logout():
        user = g.auth_user
        token = session.get("auth_session")
        database = get_database()
        if isinstance(token, str):
            database.auth_sessions.update_one(
                {
                    "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    "user_id": user["id"],
                    "revoked_at": None,
                },
                {"$set": {"revoked_at": utc_now()}},
            )
        session.clear()
        audit_event(database, "auth.session_revoked", user["id"])
        return jsonify({"status": "signed_out"})

    @app.get("/")
    def index():
        if not auth_user():
            return redirect("/login")
        return send_from_directory(BASE_DIR, "index.html")

    @app.get("/login")
    def login_page():
        if auth_user():
            return redirect("/")
        return send_from_directory(BASE_DIR, "login.html")

    @app.get("/terms")
    def terms_page():
        return send_from_directory(BASE_DIR, "terms.html")

    @app.get("/privacy")
    def privacy_page():
        return send_from_directory(BASE_DIR, "privacy.html")

    @app.get("/<path:filename>")
    def static_asset(filename):
        if filename not in STATIC_ASSETS:
            return jsonify({"error": "Not found."}), 404
        return send_from_directory(BASE_DIR, filename)

    @app.get("/api/health")
    def health():
        database = get_database()
        return jsonify({"status": "ok", "database": database.name})

    @app.get("/api/dashboard")
    @require_auth
    def dashboard():
        database = get_database()
        owner_id = g.auth_user["id"]
        accounts = list(database.accounts.find({"owner_id": owner_id}, {"_id": 0}).sort("currency", 1))
        transactions = [
            serialize_transaction(transaction)
            for transaction in database.transactions.find({"owner_id": owner_id}, {"_id": 0})
            .sort("created_at", -1)
            .limit(100)
        ]
        recipients = [
            recipient["name"]
            for recipient in database.recipients.find(
                {"owner_id": owner_id}, {"_id": 0, "name": 1}
            ).sort("name", 1)
        ]
        bank_links = [
            {
                "institution": link.get("institution", "External bank"),
                "currency": link.get("currency", ""),
                "masked_number": link.get("masked_number", ""),
                "status": link.get("status", "pending"),
            }
            for link in database.bank_links.find(
                {"owner_id": owner_id},
                {"_id": 0, "institution": 1, "currency": 1, "masked_number": 1, "status": 1},
            ).sort("created_at", -1)
        ]
        return jsonify({
            "accounts": accounts,
            "transactions": transactions,
            "recipients": recipients,
            "bank_links": bank_links,
            "demo_data": False,
        })

    @app.get("/api/integrations/status")
    def integration_status():
        paystack_key = app.config["PAYSTACK_SECRET_KEY"]
        mono_ready = bool(app.config["MONO_SECRET_KEY"] and app.config["MONO_WEBHOOK_SECRET"])
        smile_ready = bool(app.config["SMILE_ID_PARTNER_ID"] and app.config["SMILE_ID_API_KEY"])
        smile_ready = smile_ready and bool(
            app.config["SMILE_ID_LOGO_URL"]
            and app.config["APP_BASE_URL"].startswith("https://")
            and app.config["APP_PRIVACY_URL"].startswith("https://")
            and app.config["SMILE_ID_ENVIRONMENT"] in {"sandbox", "production"}
        )
        resend_ready = bool(app.config["RESEND_API_KEY"] and app.config["RESEND_FROM_EMAIL"])
        termii_ready = bool(
            app.config["TERMII_API_KEY"]
            and app.config["TERMII_SENDER_ID"]
            and app.config["TERMII_API_URL"]
        )
        return jsonify({
            "paystack": "sandbox configured" if paystack_key.startswith("sk_test_") else "not configured",
            "exchange_rates": "ExchangeRate-API daily indicative rates",
            "bank_linking": "ready" if mono_ready else "not configured",
            "identity_verification": "ready" if smile_ready else "not configured",
            "notifications": "credentials configured" if resend_ready and termii_ready else "not configured",
            "email_notifications": "credentials configured" if resend_ready else "not configured",
            "sms_notifications": "credentials configured" if termii_ready else "not configured",
            "support": "ready" if app.config["CRISP_WEBSITE_ID"] else "not configured",
            "crisp_website_id": app.config["CRISP_WEBSITE_ID"],
            "privacy_url": app.config["APP_PRIVACY_URL"],
        })

    @app.post("/api/kyc/session")
    @require_auth
    @limiter.limit("3 per day")
    def create_kyc_session():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("consent") is not True:
            return jsonify({"error": "Review the privacy notice and explicitly consent before starting identity verification."}), 400
        privacy_url = app.config["APP_PRIVACY_URL"]
        if not privacy_url.startswith("https://"):
            raise ProviderError("Set a public HTTPS APP_PRIVACY_URL before starting identity verification.")
        user = g.auth_user
        consent = {
            "granted": True,
            "granted_at": utc_now().isoformat(),
            "notice_language": "EN",
            "notice_privacy_policy_url": privacy_url,
        }
        database = get_database()
        smile_user_id, token = smile_token(user, consent)
        now = utc_now()
        database.kyc_jobs.insert_one({
            "smile_user_id": smile_user_id,
            "owner_id": user["id"],
            "status": "pending",
            "consent_granted_at": now,
            "created_at": now,
        })
        database.users.update_one(
            {"id": user["id"]},
            {"$set": {"kyc_status": "pending", "kyc_consent_at": now, "updated_at": now}},
        )
        audit_event(database, "kyc.consent_granted", user["id"], {
            "provider": "smile_id",
            "privacy_notice_url": privacy_url,
            "consent_language": "EN",
        })
        return jsonify({
            "token": token,
            "environment": app.config["SMILE_ID_ENVIRONMENT"],
            "callback_url": f"{app.config['APP_BASE_URL']}/api/kyc/smile-webhook",
            "partner_details": {
                "partner_id": app.config["SMILE_ID_PARTNER_ID"],
                "name": app.config["SMILE_ID_PARTNER_NAME"],
                "logo_url": app.config["SMILE_ID_LOGO_URL"],
                "policy_url": privacy_url,
                "theme_color": app.config["SMILE_ID_THEME_COLOR"],
            },
        }), 201

    @app.get("/api/kyc/status")
    @require_auth
    def kyc_status():
        user = g.auth_user
        database = get_database()
        jobs = [
            {
                "status": job["status"],
                "created_at": job["created_at"].isoformat(),
                "completed_at": job.get("completed_at").isoformat() if job.get("completed_at") else None,
            }
            for job in database.kyc_jobs.find(
                {"owner_id": user["id"]},
                {"_id": 0, "status": 1, "created_at": 1, "completed_at": 1},
            ).sort("created_at", -1).limit(5)
        ]
        return jsonify({"status": user.get("kyc_status", "not_started"), "jobs": jobs})

    @app.post("/api/kyc/smile-webhook")
    @csrf.exempt
    def smile_webhook():
        api_key = app.config["SMILE_ID_API_KEY"]
        partner_id = app.config["SMILE_ID_PARTNER_ID"]
        if not api_key or not partner_id:
            return jsonify({"error": "Smile ID webhook is not configured."}), 503
        signature = request.headers.get("Response-Signature", "")
        timestamp = request.headers.get("Response-Timestamp", "")
        if not signature or not timestamp:
            return jsonify({"error": "Missing Smile ID webhook signature."}), 401
        signed_content = f"{timestamp}{partner_id}sid_request".encode("utf-8")
        expected = base64.b64encode(
            hmac.new(api_key.encode("utf-8"), signed_content, hashlib.sha256).digest()
        ).decode("ascii")
        if not hmac.compare_digest(expected, signature):
            return jsonify({"error": "Invalid Smile ID webhook signature."}), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("product") != "biometric_kyc":
            return jsonify({"error": "Invalid Smile ID KYC webhook payload."}), 400
        partner_params = payload.get("partner_params")
        if not isinstance(partner_params, dict):
            return jsonify({"error": "Smile ID webhook is missing partner metadata."}), 400
        smile_user_id = partner_params.get("user_id") or request.headers.get("user-id")
        job_id = partner_params.get("job_id") or request.headers.get("smileid-job-id")
        if not isinstance(smile_user_id, str) or not isinstance(job_id, str):
            return jsonify({"error": "Smile ID webhook is missing job identifiers."}), 400
        result_status = payload.get("status")
        if not isinstance(result_status, str):
            return jsonify({"error": "Smile ID webhook is missing a verification status."}), 400
        result_status = result_status.casefold()
        if result_status == "clear":
            status = "verified"
        elif result_status in {"rejected", "failed"}:
            status = "rejected"
        else:
            status = "review"

        database = get_database()
        job = database.kyc_jobs.find_one({"smile_user_id": smile_user_id})
        if not job:
            return jsonify({"status": "ignored"})
        now = utc_now()
        update = database.kyc_jobs.update_one(
            {"_id": job["_id"], "job_id": {"$exists": False}},
            {"$set": {"job_id": job_id, "status": status, "completed_at": now}},
        )
        if update.modified_count:
            database.users.update_one(
                {"id": job["owner_id"]},
                {"$set": {"kyc_status": status, "updated_at": now}},
            )
            audit_event(database, "kyc.result_received", job["owner_id"], {
                "provider": "smile_id",
                "status": status,
            })
        elif job.get("job_id") != job_id:
            return jsonify({"error": "Smile ID job identifier did not match the verification session."}), 409
        return jsonify({"status": "processed"})

    @app.post("/api/bank-links")
    @require_auth
    @limiter.limit("5 per hour")
    def create_bank_link():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send a JSON object with your name and email address."}), 400
        name = payload.get("name")
        email = payload.get("email")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            return jsonify({"error": "Enter your name (up to 100 characters)."}), 400
        if (
            not isinstance(email, str)
            or len(email) > 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip())
        ):
            return jsonify({"error": "Enter a valid email address."}), 400

        database = get_database()
        reference = f"soltor_{uuid.uuid4().hex}"
        now = utc_now()
        database.bank_links.insert_one({
            "reference": reference,
            "owner_id": g.auth_user["id"],
            "status": "initiating",
            "created_at": now,
            "updated_at": now,
        })
        redirect_url = (
            f"{app.config['APP_BASE_URL']}/bank-link/callback"
            if app.config["APP_BASE_URL"]
            else url_for("bank_link_callback", _external=True)
        )
        try:
            result = mono_request(
                "POST",
                "/v2/accounts/initiate",
                {
                    "customer": {"name": name.strip(), "email": email.strip().lower()},
                    "meta": {"ref": reference},
                    "scope": "auth",
                    "redirect_url": redirect_url,
                },
            )
        except ProviderError:
            database.bank_links.update_one(
                {"reference": reference},
                {"$set": {"status": "failed", "updated_at": utc_now()}},
            )
            raise

        mono_url = result.get("mono_url")
        parsed_url = urllib.parse.urlparse(mono_url if isinstance(mono_url, str) else "")
        if parsed_url.scheme != "https" or parsed_url.hostname != "link.mono.co":
            database.bank_links.update_one(
                {"reference": reference},
                {"$set": {"status": "failed", "updated_at": utc_now()}},
            )
            raise ProviderError("Mono returned an unexpected bank-link address.")
        database.bank_links.update_one(
            {"reference": reference},
            {"$set": {"status": "linking", "updated_at": utc_now()}},
        )
        return jsonify({"mono_url": mono_url}), 201

    @app.get("/bank-link/callback")
    def bank_link_callback():
        return redirect("/?bank_link=returned", code=303)

    @app.post("/api/bank-links/webhook")
    @csrf.exempt
    def mono_webhook():
        webhook_secret = app.config["MONO_WEBHOOK_SECRET"]
        if not webhook_secret:
            return jsonify({"error": "Mono webhook is not configured."}), 503
        supplied_secret = request.headers.get("mono-webhook-secret", "")
        if not hmac.compare_digest(webhook_secret, supplied_secret):
            return jsonify({"error": "Invalid Mono webhook secret."}), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
            return jsonify({"error": "Invalid Mono webhook payload."}), 400
        event = payload["event"]
        if event not in {
            "mono.events.account_connected",
            "mono.events.account_updated",
            "mono.events.account_disconnected",
        }:
            return jsonify({"status": "ignored"})
        data = payload.get("data")
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid Mono webhook account data."}), 400
        account = data.get("account") if isinstance(data.get("account"), dict) else data
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        if not meta and isinstance(account.get("meta"), dict):
            meta = account["meta"]
        reference = meta.get("ref")
        account_id = data.get("id") or account.get("_id") or account.get("id")
        if not isinstance(reference, str) and not isinstance(account_id, str):
            return jsonify({"error": "Mono webhook did not include an account reference."}), 400
        database = get_database()
        query = {"reference": reference} if isinstance(reference, str) else {"account_id": account_id}
        if isinstance(account_id, str):
            query = {"$or": [{"reference": reference}, {"account_id": account_id}]} if isinstance(reference, str) else query
        link = database.bank_links.find_one(query)
        if not link:
            return jsonify({"status": "ignored"})

        data_status = meta.get("data_status")
        status = "disconnected" if event.endswith("account_disconnected") else (
            data_status.lower() if isinstance(data_status, str) else (
                "linked" if event.endswith("account_connected") else link.get("status", "pending")
            )
        )
        updates = {"status": status, "updated_at": utc_now()}
        if isinstance(account_id, str):
            updates["account_id"] = account_id
        institution = account.get("institution")
        if isinstance(institution, dict) and isinstance(institution.get("name"), str):
            updates["institution"] = institution["name"][:100]
        currency = account.get("currency")
        if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency):
            updates["currency"] = currency
        account_number = account.get("accountNumber") or account.get("account_number")
        if isinstance(account_number, str) and len(account_number) >= 4:
            updates["masked_number"] = f"•••• {account_number[-4:]}"
        database.bank_links.update_one({"_id": link["_id"]}, {"$set": updates})
        return jsonify({"status": "processed"})

    @app.get("/api/payments/banks")
    def payment_banks():
        banks = []
        for page in range(1, 21):
            batch = paystack_request(
                "GET",
                "/bank",
                query={"country": "nigeria", "currency": "NGN", "perPage": 100, "page": page},
            )
            banks.extend(batch)
            if len(batch) < 100:
                break
        return jsonify({
            "banks": [
                {"name": bank["name"], "code": bank["code"]}
                for bank in banks
                if bank.get("active") and bank.get("supports_transfer")
            ]
        })

    @app.post("/api/payments/initialize")
    @require_auth
    @limiter.limit("10 per hour")
    def initialize_payment():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send a JSON object with an email and NGN amount."}), 400
        email = payload.get("email")
        if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
            return jsonify({"error": "Enter a valid customer email address."}), 400
        try:
            amount_minor = parse_ngn_amount(payload)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        database = get_database()
        reference = f"soltor_{uuid.uuid4().hex}"
        attempt = {
            "reference": reference,
            "owner_id": g.auth_user["id"],
            "email": email.strip().lower(),
            "amount_minor": amount_minor,
            "currency": "NGN",
            "status": "pending",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        database.payment_attempts.insert_one(attempt)
        callback_url = (
            f"{app.config['APP_BASE_URL']}/payment/callback"
            if app.config["APP_BASE_URL"]
            else url_for("payment_callback", _external=True)
        )
        try:
            checkout = paystack_request(
                "POST",
                "/transaction/initialize",
                {
                    "email": attempt["email"],
                    "amount": amount_minor,
                    "currency": "NGN",
                    "reference": reference,
                    "callback_url": callback_url,
                    "metadata": {"source": "Soltor sandbox payment collection"},
                },
            )
        except ProviderError as error:
            database.payment_attempts.update_one(
                {"reference": reference},
                {"$set": {"status": "failed", "provider_error": str(error), "updated_at": utc_now()}},
            )
            raise

        database.payment_attempts.update_one(
            {"reference": reference},
            {"$set": {"access_code": checkout["access_code"], "authorization_url": checkout["authorization_url"]}},
        )
        return jsonify({
            "reference": reference,
            "authorization_url": checkout["authorization_url"],
            "amount_minor": amount_minor,
            "currency": "NGN",
        }), 201

    @app.get("/payment/callback")
    def payment_callback():
        if not auth_user():
            return redirect("/login")
        reference = request.args.get("reference", "")
        if not reference or len(reference) > 100:
            return redirect("/?payment=invalid")
        database = get_database()
        attempt = database.payment_attempts.find_one({"reference": reference})
        if not attempt or attempt.get("owner_id") != g.auth_user["id"]:
            return redirect("/?payment=unknown")
        if attempt["status"] != "success":
            try:
                verified = paystack_request("GET", f"/transaction/verify/{urllib.parse.quote(reference, safe='')}")
                record_verified_payment(database, reference, verified)
            except ProviderError:
                database.payment_attempts.update_one(
                    {"reference": reference, "status": "pending"},
                    {"$set": {"status": "failed", "updated_at": utc_now()}},
                )
                return redirect("/?payment=failed")
        return redirect("/?payment=success")

    @app.post("/api/payments/webhook")
    @csrf.exempt
    def paystack_webhook():
        secret_key = require_paystack()
        signature = request.headers.get("x-paystack-signature", "")
        expected_signature = hmac.new(secret_key.encode(), request.get_data(), hashlib.sha512).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected_signature):
            return jsonify({"error": "Invalid Paystack webhook signature."}), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"status": "ignored"})
        event_data = payload.get("data")
        if not isinstance(event_data, dict) or not isinstance(event_data.get("reference"), str):
            return jsonify({"error": "Invalid payment webhook payload."}), 400
        database = get_database()
        if payload["event"] == "charge.success":
            record_verified_payment(database, event_data["reference"], event_data)
        elif payload["event"] in {"transfer.success", "transfer.failed", "transfer.reversed"}:
            status = payload["event"].split(".", maxsplit=1)[1]
            record_transfer_status(database, event_data, status)
        else:
            return jsonify({"status": "ignored"})
        return jsonify({"status": "processed"})

    @app.post("/api/transfers/resolve-account")
    @require_auth
    @limiter.limit("10 per hour")
    def resolve_transfer_account():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send the bank code and account number as JSON."}), 400
        account_number = payload.get("account_number", "")
        bank_code = payload.get("bank_code", "")
        if not re.fullmatch(r"\d{10}", account_number) or not re.fullmatch(r"\d{2,10}", bank_code):
            return jsonify({"error": "Enter a 10-digit account number and select a valid bank."}), 400
        resolved = paystack_request(
            "GET",
            "/bank/resolve",
            query={"account_number": account_number, "bank_code": bank_code},
        )
        return jsonify({"account_name": resolved["account_name"]})

    @app.post("/api/transfers")
    @require_auth
    @limiter.limit("5 per hour")
    def create_transfer():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send a JSON object with the bank details and amount."}), 400
        recipient = payload.get("recipient")
        account_number = payload.get("account_number")
        bank_code = payload.get("bank_code")
        reason = payload.get("reason", "Soltor sandbox transfer")
        if not isinstance(recipient, str) or not recipient.strip() or len(recipient.strip()) > 100:
            return jsonify({"error": "Enter a recipient name of 1 to 100 characters."}), 400
        if not isinstance(account_number, str) or not re.fullmatch(r"\d{10}", account_number):
            return jsonify({"error": "Enter a valid 10-digit Nigerian bank account number."}), 400
        if not isinstance(bank_code, str) or not re.fullmatch(r"\d{2,10}", bank_code):
            return jsonify({"error": "Select a valid Nigerian bank."}), 400
        if not isinstance(reason, str) or len(reason.strip()) > 100:
            return jsonify({"error": "Enter a transfer reason up to 100 characters."}), 400
        try:
            amount_minor = parse_ngn_amount(payload)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        database = get_database()
        reference = f"soltor_{uuid.uuid4().hex}"
        try:
            resolved = paystack_request(
                "GET",
                "/bank/resolve",
                query={"account_number": account_number, "bank_code": bank_code},
            )
            if resolved["account_name"].casefold() != recipient.strip().casefold():
                return jsonify({
                    "error": "Recipient name does not match the verified bank account name.",
                    "verified_account_name": resolved["account_name"],
                }), 400
            database.transfers.insert_one({
                "reference": reference,
                "owner_id": g.auth_user["id"],
                "recipient_name": resolved["account_name"],
                "account_number_masked": f"••••••{account_number[-4:]}",
                "bank_code": bank_code,
                "amount_minor": amount_minor,
                "currency": "NGN",
                "status": "initiating",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            })
            recipient_result = paystack_request(
                "POST",
                "/transferrecipient",
                {
                    "type": "nuban",
                    "name": resolved["account_name"],
                    "account_number": account_number,
                    "bank_code": bank_code,
                    "currency": "NGN",
                },
            )
            result = paystack_request(
                "POST",
                "/transfer",
                {
                    "source": "balance",
                    "amount": amount_minor,
                    "recipient": recipient_result["recipient_code"],
                    "reason": reason.strip() or "Soltor sandbox transfer",
                    "reference": reference,
                },
            )
        except ProviderError as error:
            database.transfers.update_one(
                {"reference": reference},
                {"$set": {"status": "failed", "provider_error": str(error), "updated_at": utc_now()}},
            )
            raise
        result["reference"] = reference
        transfer = record_transfer_status(database, result)
        return jsonify({
            "reference": transfer["reference"],
            "status": transfer["status"],
            "message": (
                "Paystack sandbox requested an OTP. Enter the code to complete the test."
                if transfer["status"] == "otp"
                else "Sandbox transfer submitted; wait for Paystack's final status. No live funds were moved."
            ),
        }), 201

    @app.post("/api/transfers/finalize")
    @require_auth
    @limiter.limit("5 per hour")
    def finalize_transfer():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send a transfer reference and OTP."}), 400
        reference = payload.get("reference")
        otp = payload.get("otp")
        if not isinstance(reference, str) or not re.fullmatch(r"soltor_[a-f0-9]{32}", reference):
            return jsonify({"error": "Enter a valid transfer reference."}), 400
        if not isinstance(otp, str) or not re.fullmatch(r"\d{4,8}", otp):
            return jsonify({"error": "Enter the OTP provided by Paystack."}), 400
        database = get_database()
        transfer = database.transfers.find_one({"reference": reference})
        if (
            not transfer
            or transfer.get("owner_id") != g.auth_user["id"]
            or not transfer.get("transfer_code")
        ):
            return jsonify({"error": "This transfer is not waiting for an OTP."}), 409
        result = paystack_request(
            "POST",
            "/transfer/finalize_transfer",
            {"transfer_code": transfer["transfer_code"], "otp": otp},
        )
        result["reference"] = reference
        saved = record_transfer_status(database, result)
        return jsonify({
            "reference": saved["reference"],
            "status": saved["status"],
            "message": "OTP submitted to Paystack sandbox; await the final transfer status.",
        })

    @app.get("/api/fx/quote")
    def exchange_quote():
        source = request.args.get("from", "").upper()
        target = request.args.get("to", "").upper()
        if source not in SUPPORTED_CURRENCIES or target not in SUPPORTED_CURRENCIES:
            return jsonify({"error": "Choose a supported ISO currency code."}), 400
        try:
            amount = Decimal(request.args.get("amount", "1"))
            if not amount.is_finite() or amount <= 0 or amount > Decimal("100000000"):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            return jsonify({"error": "Enter a positive amount up to 100,000,000."}), 400

        if source == target:
            return jsonify({
                "from": source,
                "to": target,
                "amount": float(amount),
                "rate": 1,
                "result": float(amount),
                "provider": "Soltor",
                "updated_at": utc_now().isoformat(),
            })
        cache = app.config["EXCHANGE_RATE_CACHE"]
        cached = cache.get(source)
        if cached and cached["expires_at"] > time.monotonic():
            result = cached["result"]
        else:
            result = get_json(f"{EXCHANGE_RATE_API_URL}/{urllib.parse.quote(source, safe='')}")
            cache[source] = {"result": result, "expires_at": time.monotonic() + 900}
        if result.get("result") != "success" or result.get("base_code") != source:
            raise ProviderError("The exchange-rate provider could not quote that currency.")
        rate = result.get("rates", {}).get(target)
        if not isinstance(rate, (int, float)):
            raise ProviderError(f"A reference rate for {source} to {target} is unavailable.")
        return jsonify({
            "from": source,
            "to": target,
            "amount": float(amount),
            "rate": rate,
            "result": float(amount) * rate,
            "provider": "ExchangeRate-API",
            "updated_at": result.get("time_last_update_utc"),
            "indicative_only": True,
        })

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
