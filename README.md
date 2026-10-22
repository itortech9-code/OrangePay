# Soltor fintech demo

Soltor is a local fintech integration demo backed by MongoDB. It stores sample account data, customer payment attempts, payout statuses, and transaction history in the `soltor_fintech` database. Its Paystack integration is restricted to test secret keys; it cannot make live charges or payouts.

## Run locally

1. Install MongoDB Community Server and start the MongoDB service, or create a MongoDB Atlas database.
2. A local `.env` is ready for the provided Atlas cluster and Paystack sandbox. Replace `REPLACE_WITH_URL_ENCODED_DB_PASSWORD` in `MONGODB_URI` with your database user's password. URL-encode reserved characters in the password (for example, `@` as `%40`). Keep credentials private and do not commit `.env`.
   Add provider credentials only to this local file when you are ready to configure them. Mono bank linking also needs a webhook secret from the Mono dashboard. Set `APP_BASE_URL` to the public HTTPS URL when testing provider redirects and webhooks.
3. Install the Python dependencies and start the dashboard:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python app.py
   ```

4. Open <http://127.0.0.1:5000>.

The database and its collections are created automatically on the first successful API request. Seed accounts, recipients, and sample transactions are inserted only when their collections are empty. Set `MONGODB_DATABASE` in `.env` to use a different database name. The sample balances and sample transaction rows are illustrative and are not synced to Paystack.

## API

- `GET /api/health` — checks the MongoDB connection.
- `GET /api/dashboard` — returns accounts, recipients, the latest 100 transactions, and masked Mono link status.
- `GET /api/integrations/status` — shows provider configuration without exposing credentials.
- `POST /api/bank-links` — starts a Mono Connect Link flow. The customer is redirected to Mono; Soltor does not receive bank login credentials.
- `GET /bank-link/callback` — returns the customer to the dashboard after Mono's hosted flow.
- `POST /api/bank-links/webhook` — validates Mono's `mono-webhook-secret` header and stores only the provider account ID, institution, currency, connection status, and a masked account number.
- `GET /api/payments/banks` — retrieves supported Nigerian payout banks from Paystack.
- `POST /api/payments/initialize` — creates an NGN Paystack test checkout. A successful checkout is verified before it appears in activity.
- `POST /api/payments/webhook` — verifies Paystack's HMAC-SHA512 signature before processing payment or transfer events. Configure this URL in Paystack using a public HTTPS URL before expecting asynchronous updates.
- `POST /api/transfers/resolve-account` — verifies Nigerian bank account details through Paystack without putting account numbers in URLs.
- `POST /api/transfers` — initiates an NGN Paystack sandbox payout after checking the bank account name.
- `POST /api/transfers/finalize` — submits the Paystack transfer OTP when the sandbox requires one.
- `GET /api/fx/quote?from=NGN&to=USD&amount=10000` — fetches a daily indicative reference rate. It is not a guaranteed payment quote and does not execute an exchange.

The dashboard shows a visible connection error when MongoDB is unavailable. It does not silently substitute browser-only demo data. Paystack API requests use only a `sk_test_` key. The test secret shared in chat should be rotated before further use; replace the local `PAYSTACK_SECRET_KEY` with the newly generated test key. Crisp chat is loaded only after the user selects Get support and requires a public Website ID.

Mono bank connection and Crisp support are integrated when their credentials are configured. Smile ID KYC and Resend/Termii notifications are not yet wired into user flows; their credential fields are listed for future setup, but adding credentials alone does not enable those services. FX rates come from [ExchangeRate-API's free reference-rate endpoint](https://www.exchangerate-api.com/docs/free), refreshed daily.

Do not deploy this demo publicly. User authentication and authorization, CSRF protections, compliance/KYC workflows, rate limiting, reconciliation, and production payment controls are not implemented. Mono bank connections are displayed as linked-account metadata only; they do not populate balances or transactions.
