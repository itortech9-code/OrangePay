# Soltor

Soltor is a Flask fintech application scaffold for international money services. It uses MongoDB for persistent records and provides account access, consent-based identity verification, Paystack sandbox payments, Mono bank-link initiation, and indicative FX quotes.

**This is not approved for live-money use.** Paystack is restricted to test keys. Production deployment remains gated on provider credentials and approvals, legal/compliance review, operational controls, and verification of the target infrastructure. Do not use real customer funds or identity data until those blockers are resolved.

## Run locally

1. Install MongoDB Community Server and start it, or provision a MongoDB Atlas database.
2. Copy `.env.example` to `.env`. Set `MONGODB_URI`, a random `SECRET_KEY` of at least 32 characters, and provider settings in your local environment file. URL-encode reserved characters in MongoDB passwords. Never commit `.env` or share credentials in chat.
3. Install dependencies and run the app:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python app.py
   ```

4. Visit <http://127.0.0.1:5000>. Run the focused security tests with:

   ```powershell
   python -m pip install -r requirements-dev.txt
   python -m pytest -q
   ```

Mongo collections and indexes are initialized automatically. Authenticated users only see records associated with their own user ID; no fake balances are created for new accounts. Existing legacy demo records without an owner are not returned to authenticated users.

## Account access and identity verification

- Registration accepts either an email address or an international-format phone number. Passwords are hashed; login and contact verification require a one-time code.
- Configure Resend (`RESEND_API_KEY`, `RESEND_FROM_EMAIL`) for email codes or Termii (`TERMII_API_KEY`, `TERMII_SENDER_ID`, `TERMII_API_URL`) for phone codes. Provider credentials must be set before those delivery methods can be used.
- Terms of Service and Privacy Notice are published at `/terms` and `/privacy`. Set `APP_TERMS_URL` and `APP_PRIVACY_URL` to their public HTTPS URLs to enable account creation. Keep these notices current as the service and its data practices change.
- The first administrator must register and verify their own account normally. Temporarily configure a random `ADMIN_BOOTSTRAP_TOKEN` of at least 32 characters in the private deployment environment, sign in with that account, and visit `/admin/setup` to claim the one-time administrator role. Remove the token from the deployment environment immediately after claiming; the database permanently records the first claim. The `/admin` workspace provides read-only account-support search (name, contact, account status, and verification status); it does not expose identity documents, balances, transactions, or payment actions. Never share the bootstrap token or account password in chat.
- Smile ID uses its hosted SDK flow. Configure `SMILE_ID_PARTNER_ID`, `SMILE_ID_API_KEY`, and the relevant environment and callback settings. The signed provider webhook is authoritative. Soltor stores consent and verification status metadata, not submitted identity fields. Never collect raw BVN/NIN in Soltor forms or store Smile ID `id_fields`.
- A successful local test run does not verify live email/SMS delivery, Smile ID credentials, webhook reachability, or legal compliance.

## Integrations

- **Paystack:** `PAYSTACK_SECRET_KEY` must be a test secret (`sk_test_`). Checkout and payout flows are sandbox-only. Configure `/api/payments/webhook` over HTTPS for asynchronous sandbox events. Rotate any test credential that has been exposed.
- **Mono:** set `MONO_SECRET_KEY` and `MONO_WEBHOOK_SECRET`; configure the public callback/webhook URLs with Mono. Bank links retain provider account metadata only and do not create real account balances or imported transaction histories.
- **Smile ID:** see the identity verification section. The API key remains server-side; webhook signatures are validated before status changes.
- **Crisp:** `CRISP_WEBSITE_ID` enables the support widget.
- **FX:** `/api/fx/quote` returns an indicative reference rate, not a guaranteed executable quote.

Provider status is available at `GET /api/integrations/status` without returning secret values. MongoDB connectivity is checked at `GET /api/health`.

## Render deployment readiness

`render.yaml` defines a Flask web service and shared Redis instance. GitHub Pages cannot host this Flask API. The Blueprint sets `APP_ENV=production`, generates a secret key, and intentionally leaves sensitive values for manual configuration.

Before deploying:

1. Configure a MongoDB Atlas database user with least privilege and allow network access only as required by Render. Set `MONGODB_URI` and `MONGODB_DATABASE`.
2. Set `APP_BASE_URL` to the HTTPS Render/custom-domain URL and publish HTTPS Terms and Privacy pages at `APP_TERMS_URL` and `APP_PRIVACY_URL`.
3. Confirm Redis is provisioned and reachable through `REDIS_URL`; production refuses in-memory rate-limit storage.
4. Add verified Resend/Termii credentials and approved Smile ID credentials in Render’s secret environment settings. Add Paystack sandbox and optional Mono/Crisp values only when configured.
5. To provision the first admin, add a private, random `ADMIN_BOOTSTRAP_TOKEN` (at least 32 characters), deploy, then have the first verified admin sign in and claim access at `/admin/setup`. Remove the variable and redeploy after the claim.
6. Configure provider callback and webhook URLs, verify signatures and delivery using provider sandboxes, and confirm Atlas, Redis, health checks, rate limits, and secure cookies in the deployed environment.
7. Before any live-money release, complete independent security review, legal and licensing review, provider production approval, payment reconciliation and retry operations, incident response, privacy/retention and user data-rights procedures, and tested backup/restore monitoring.

The presence of a Render Blueprint is not proof of a successful deployment or production readiness. No credentials or live service access have been configured or verified in this repository.

## Security notes

Production startup requires HTTPS URLs, a strong secret, MongoDB, and shared Redis. Mutating browser requests require CSRF protection and same-origin validation. Sessions are opaque, MongoDB-backed, revocable, and expire. Payment and KYC webhook routes authenticate provider requests. Paystack remains sandbox-only; do not enable live keys without completing the launch blockers above.
