# Wholesale Scout

Wholesale Scout is a property deal calculator with a separate PDF Studio. The original calculator remains on the home page, now with live estimates and sample deals.

## Web application

Install dependencies and start the Flask app:

```powershell
python -m pip install -r requirements.txt
python web_app.py
```

Open `http://127.0.0.1:5000`. The home page calculates rental yields and flip returns for single-family rentals, apartments, HMOs, renovations/flips, and commercial properties. Example deals fill the calculator so figures can be explored immediately.

Open `/pdf-studio` to preview and download a property-specific PDF report. Downloads contain fillable PDF form fields for the address, deal figures, rating, and notes, so the saved report can also be edited in a PDF reader. Edits made directly in a PDF reader do not recalculate the report's yield/profit summary; update the figures in PDF Studio and download a fresh report to recalculate. Estimates are illustrative, not financial advice.

## Project files

The Render service root is the repository root (`.`). Keep `render.yaml`, `requirements.txt`, `app.py`, and `web_app.py` at this top level; keep browser assets in `static/`, HTML templates in `templates/`, and tests in `tests/`. Render installs packages from the root `requirements.txt` and starts the Flask app with `gunicorn web_app:app`.

## Monthly PDF subscriptions and payment setup

The calculator, live examples, and PDF previews are free. Downloading a fillable PDF requires an active monthly subscription matching its property type. Each matching plan also unlocks its sample property report PDF; one plan does not unlock another property's sample. Current prices are £8.99/month for single-family and apartment rentals, £12.99/month for HMOs and renovation/flips, and £16.99/month for commercial property. Card and PayPal checkout are hosted by their providers; the app never collects or stores card details.

Configure the Flask server using the hosting provider's environment/secret settings. Do not put payment secrets in source control:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Persistent PostgreSQL connection string (required on Render; local development defaults to SQLite) |
| `FLASK_SECRET_KEY` | Long random value used to sign browser sessions |
| `PUBLIC_BASE_URL` | Canonical HTTPS URL used for Stripe return/cancel redirects |
| `STRIPE_SECRET_KEY` | Stripe server-side key (`sk_test_…` for tests, `sk_live_…` for launch) |
| `STRIPE_WEBHOOK_SECRET` | Signing secret for the Stripe endpoint `/webhooks/stripe` |
| `STRIPE_PRICE_SINGLE_FAMILY_RENTAL` | Active GBP £8.99 recurring monthly Stripe Price ID |
| `STRIPE_PRICE_APARTMENT_RENTAL` | Active GBP £8.99 recurring monthly Stripe Price ID |
| `STRIPE_PRICE_HMO` | Active GBP £12.99 recurring monthly Stripe Price ID |
| `STRIPE_PRICE_FLIP` | Active GBP £12.99 recurring monthly Stripe Price ID |
| `STRIPE_PRICE_COMMERCIAL` | Active GBP £16.99 recurring monthly Stripe Price ID |
| `STRIPE_FOUNDERS_COUPON_ID` | Optional Stripe coupon ID for the first ten card subscriptions |
| `PAYPAL_CLIENT_ID` | PayPal REST app client ID |
| `PAYPAL_CLIENT_SECRET` | PayPal app secret; server-side only |
| `PAYPAL_ENV` | `sandbox` for testing (default) or `live` after verification |
| `PAYPAL_CURRENCY` | Three-letter currency; use `GBP` for the listed plans |
| `PAYPAL_WEBHOOK_ID` | PayPal webhook ID registered for `/webhooks/paypal` |
| `PAYPAL_PLAN_SINGLE_FAMILY_RENTAL` | PayPal active monthly GBP £8.99 plan ID |
| `PAYPAL_PLAN_APARTMENT_RENTAL` | PayPal active monthly GBP £8.99 plan ID |
| `PAYPAL_PLAN_HMO` | PayPal active monthly GBP £12.99 plan ID |
| `PAYPAL_PLAN_FLIP` | PayPal active monthly GBP £12.99 plan ID |
| `PAYPAL_PLAN_COMMERCIAL` | PayPal active monthly GBP £16.99 plan ID |
| `WEB_APP_URL` | Public Flask URL used by the separate Streamlit Subscribe page |

Create one active, fixed-amount monthly GBP Stripe Price for each property plan in Stripe, then set the corresponding Price IDs. The server checks the configured currency, amount, and billing interval against the displayed price before starting hosted Checkout. Live keys require `PUBLIC_BASE_URL` to use HTTPS.

The optional founding offer is for card checkout only: create a Stripe coupon with a 25% discount, repeating for 3 months, and a maximum of 10 redemptions; set its ID as `STRIPE_FOUNDERS_COUPON_ID`. The app checks these coupon properties and available redemptions with Stripe before applying it. Without this variable, there is no advertised discount.

In PayPal, create an app and an active monthly billing plan for each tier, using the exact GBP amount in the table; set each plan ID and the app credentials above. The browser SDK starts PayPal Subscriptions, then the Flask server retrieves and checks the subscription and its configured plan before granting PDF access. Register a PayPal webhook URL at `https://<your-domain>/webhooks/paypal` for `BILLING.SUBSCRIPTION.ACTIVATED`, `BILLING.SUBSCRIPTION.UPDATED`, `BILLING.SUBSCRIPTION.SUSPENDED`, `BILLING.SUBSCRIPTION.CANCELLED`, and `BILLING.SUBSCRIPTION.EXPIRED`; put its webhook ID in `PAYPAL_WEBHOOK_ID`. The server asks PayPal to validate each webhook signature before syncing subscription state.

Customer accounts use scrypt-hashed passwords and store subscription state in the configured database. On Render, create a managed PostgreSQL database and set `DATABASE_URL` to its **internal** connection URL; the app refuses to start on Render without it, rather than silently using ephemeral local disk. Tables are initialized when the app starts. Do not use a local SQLite URL for production. Accounts are not email-verified, and self-service password reset is not implemented; customers who lose access must contact the site owner.

Register a Stripe webhook URL at `https://<your-domain>/webhooks/stripe` for `checkout.session.completed` and `customer.subscription.created`, `customer.subscription.updated`, and `customer.subscription.deleted`. Save Stripe's signing secret in `STRIPE_WEBHOOK_SECRET`. Webhook signatures are checked and subscription state is fetched from Stripe before updating the database. In Stripe's Dashboard, enable the Customer Portal with subscription cancellation and payment-method/invoice management; customers reach it from My account. PayPal customers can cancel from My account, and PayPal's signed webhooks update their status.

For Render, create a Web Service from this repository or use the included `render.yaml` Blueprint; it runs Gunicorn and checks `/health`. Create PostgreSQL separately and provide its internal URL as `DATABASE_URL`. Add all payment/account credentials as Render environment variables, never in GitHub. Check `/health` after deployment. Stripe and PayPal live credentials, billing products, and verified payout accounts are still supplied and activated by the account owner.

The site now supports accounts, provider webhook synchronization, and billing management, but still requires provider dashboard configuration and operational testing before public launch. Verify failed renewals, cancellation, webhook retries, and account recovery/support in both sandboxes. The Streamlit Subscribe page links to `/pdf-studio#plans` when `WEB_APP_URL` is configured.

## Customer assistant

The “Ask Scout” helper is a small on-site FAQ assistant for real estate agents and property investors. It uses built-in product guidance in the browser; it is not a live human or an AI service, and it does not send chat messages to a third party. Keep its claims aligned with the actual calculator, report, and checkout features.

## Payment fulfillment

Stripe and PayPal subscriptions are linked to customer accounts and unlock matching property PDFs after provider confirmation. Provider webhooks keep the durable subscription records synchronized; customers manage Stripe billing through Stripe's portal and can cancel PayPal subscriptions from My account. The app does not send billing or fulfillment emails. Email verification, password reset, and automated customer support are not implemented; publish clear support/refund terms and test recovery procedures before launch.

## Optional AI photo analysis

Set `AI_STUDIO_API_KEY` on the server to enable the existing Gemini photo-analysis feature.

## Command-line calculator

```powershell
python app.py --purchase-price 250000 --monthly-rent 1500 --sq-ft 1200 --summary "modern kitchen, needs minor updates"
```

## Tests

```powershell
python -m unittest discover -s tests
```
