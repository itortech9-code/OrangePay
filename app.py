import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory, url_for
from pymongo import MongoClient
from pymongo.errors import PyMongoError


load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
    accounts = database.accounts
    accounts.create_index("id", unique=True)
    if accounts.count_documents({}) == 0:
        accounts.insert_many(INITIAL_ACCOUNTS)

    transactions = database.transactions
    transactions.create_index("id", unique=True)
    if transactions.count_documents({}) == 0:
        now = utc_now()
        transactions.insert_many([
            {
                **{key: value for key, value in transaction.items() if key != "minutes_ago"},
                "created_at": now.replace(microsecond=0)
                - timedelta(minutes=transaction["minutes_ago"]),
            }
            for transaction in INITIAL_TRANSACTIONS
        ])

    database.recipients.create_index("name", unique=True)
    database.payment_attempts.create_index("reference", unique=True)
    database.transfers.create_index("reference", unique=True)
    database.bank_links.create_index("reference", unique=True)
    database.bank_links.create_index("account_id", sparse=True)
    for name in DEMO_RECIPIENTS:
        database.recipients.update_one(
            {"name": name},
            {"$setOnInsert": {"name": name, "demo": True}},
            upsert=True,
        )


def create_app(client=None):
    app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")
    app.config["MONGODB_URI"] = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
    app.config["MONGODB_DATABASE"] = DATABASE_NAME
    app.config["MONGODB_CLIENT"] = client
    app.config["DATABASE_INITIALIZED"] = False
    app.config["PAYSTACK_SECRET_KEY"] = os.getenv("PAYSTACK_SECRET_KEY", "")
    app.config["APP_BASE_URL"] = os.getenv("APP_BASE_URL", "").rstrip("/")
    app.config["MONO_SECRET_KEY"] = os.getenv("MONO_SECRET_KEY", "")
    app.config["MONO_WEBHOOK_SECRET"] = os.getenv("MONO_WEBHOOK_SECRET", "")
    app.config["CRISP_WEBSITE_ID"] = os.getenv("CRISP_WEBSITE_ID", "")
    app.config["SMILE_ID_PARTNER_ID"] = os.getenv("SMILE_ID_PARTNER_ID", "")
    app.config["SMILE_ID_API_KEY"] = os.getenv("SMILE_ID_API_KEY", "")
    app.config["RESEND_API_KEY"] = os.getenv("RESEND_API_KEY", "")
    app.config["RESEND_FROM_EMAIL"] = os.getenv("RESEND_FROM_EMAIL", "")
    app.config["TERMII_API_KEY"] = os.getenv("TERMII_API_KEY", "")
    app.config["TERMII_SENDER_ID"] = os.getenv("TERMII_SENDER_ID", "")
    app.config["EXCHANGE_RATE_CACHE"] = {}

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

    @app.get("/")
    def index():
        return send_from_directory(BASE_DIR, "index.html")

    @app.get("/api/health")
    def health():
        database = get_database()
        return jsonify({"status": "ok", "database": database.name})

    @app.get("/api/dashboard")
    def dashboard():
        database = get_database()
        accounts = list(database.accounts.find({}, {"_id": 0}).sort("currency", 1))
        transactions = [
            serialize_transaction(transaction)
            for transaction in database.transactions.find({}, {"_id": 0})
            .sort("created_at", -1)
            .limit(100)
        ]
        recipients = [
            recipient["name"]
            for recipient in database.recipients.find({}, {"_id": 0, "name": 1}).sort("name", 1)
        ]
        bank_links = [
            {
                "institution": link.get("institution", "External bank"),
                "currency": link.get("currency", ""),
                "masked_number": link.get("masked_number", ""),
                "status": link.get("status", "pending"),
            }
            for link in database.bank_links.find(
                {}, {"_id": 0, "institution": 1, "currency": 1, "masked_number": 1, "status": 1}
            ).sort("created_at", -1)
        ]
        return jsonify({
            "accounts": accounts,
            "transactions": transactions,
            "recipients": recipients,
            "bank_links": bank_links,
            "demo_data": True,
        })

    @app.get("/api/integrations/status")
    def integration_status():
        paystack_key = app.config["PAYSTACK_SECRET_KEY"]
        mono_ready = bool(app.config["MONO_SECRET_KEY"] and app.config["MONO_WEBHOOK_SECRET"])
        smile_ready = bool(app.config["SMILE_ID_PARTNER_ID"] and app.config["SMILE_ID_API_KEY"])
        resend_ready = bool(app.config["RESEND_API_KEY"] and app.config["RESEND_FROM_EMAIL"])
        termii_ready = bool(app.config["TERMII_API_KEY"] and app.config["TERMII_SENDER_ID"])
        return jsonify({
            "paystack": "sandbox configured" if paystack_key.startswith("sk_test_") else "not configured",
            "exchange_rates": "ExchangeRate-API daily indicative rates",
            "bank_linking": "ready" if mono_ready else "not configured",
            "identity_verification": "credentials configured" if smile_ready else "not configured",
            "notifications": "credentials configured" if resend_ready and termii_ready else "not configured",
            "email_notifications": "credentials configured" if resend_ready else "not configured",
            "sms_notifications": "credentials configured" if termii_ready else "not configured",
            "support": "ready" if app.config["CRISP_WEBSITE_ID"] else "not configured",
            "crisp_website_id": app.config["CRISP_WEBSITE_ID"],
        })

    @app.post("/api/bank-links")
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
        reference = request.args.get("reference", "")
        if not reference or len(reference) > 100:
            return redirect("/?payment=invalid")
        database = get_database()
        attempt = database.payment_attempts.find_one({"reference": reference})
        if not attempt:
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
        if not transfer or not transfer.get("transfer_code"):
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
