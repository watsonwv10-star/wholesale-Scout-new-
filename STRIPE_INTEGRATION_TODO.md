# Stripe Checkout Integration

Wholesale Scout already creates server-side Stripe Checkout Sessions for recurring subscriptions. Checkout uses Stripe-hosted pages; the app does not collect or store card details.

## Values to Replace

The following deployment configuration must be set before accepting live payments. Values are intentionally not stored in the repository.

**Files containing the Checkout Session parameters:**
- [web_app.py](web_app.py)

| Field | Current Value | What to Set |
|-------|--------------|-------------|
| `STRIPE_PRICE_SINGLE_FAMILY_RENTAL` | Unset until configured | Active GBP £8.99 recurring monthly Stripe Price ID. |
| `STRIPE_PRICE_APARTMENT_RENTAL` | Unset until configured | Active GBP £8.99 recurring monthly Stripe Price ID. |
| `STRIPE_PRICE_HMO` | Unset until configured | Active GBP £12.99 recurring monthly Stripe Price ID. |
| `STRIPE_PRICE_FLIP` | Unset until configured | Active GBP £12.99 recurring monthly Stripe Price ID. |
| `STRIPE_PRICE_COMMERCIAL` | Unset until configured | Active GBP £16.99 recurring monthly Stripe Price ID. |
| `STRIPE_SECRET_KEY` | Unset until configured | A Stripe secret key; use `sk_test_...` while testing, then `sk_live_...` for launch. |
| `STRIPE_WEBHOOK_SECRET` | Unset until configured | Signing secret for the Stripe webhook endpoint. |
| `PUBLIC_BASE_URL` | Unset until configured | The site's canonical public URL. Use HTTPS with live Stripe keys. |

`mode`, `success_url`, `cancel_url`, and `line_items` are already populated by the existing subscription flow. Success and cancellation URLs use `PUBLIC_BASE_URL`; line items use the configured Stripe Price ID for the selected property type. The app validates that each Price is active, monthly, and matches the displayed GBP amount before starting Checkout.

## Configured Parameters

**Files containing these parameters:**
- [web_app.py](web_app.py)

| Parameter | Value |
|-----------|-------|
| `ui_mode` | `hosted_page` |
| `billing_address_collection` | `auto` |
| `allow_promotion_codes` | `false` |
| `payment_method_collection` | `always` |
| `submit_type` | `auto` |
| `integration_identifier` | `hosted_web_0002` |
| `origin_context` | `web` |
| `mode` | `subscription` |

`web_app.py` sends requests directly to Stripe's REST API and does not use the Stripe SDK, so there is no installed SDK version to check. The requested `ui_mode` value is `hosted_page`; verify it against the Stripe API version/account configuration used for deployment.

## Setup and Next Steps

1. In Stripe Dashboard, create one active GBP recurring monthly Price for each property plan and set the corresponding `STRIPE_PRICE_*` variables in the hosting provider's secret/environment settings.
2. Set `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and `PUBLIC_BASE_URL` in the same server-side environment. Never put secret keys in source control or browser-accessible configuration.
3. Register `https://<your-domain>/webhooks/stripe` in Stripe for `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, and `customer.subscription.deleted`. Store the endpoint signing secret as `STRIPE_WEBHOOK_SECRET`.
4. Configure and test Stripe Customer Portal in the Dashboard if customers should manage billing there. The app links it from My account.
5. Run the existing tests with `python -m unittest discover -s tests`. Test using Stripe test mode and test Price IDs before changing to live credentials.

For a standard successful test payment, Stripe documents the test card `4242 4242 4242 4242`; use any future expiry date and any three-digit CVC in test mode. Use Stripe's official testing guide for other scenarios: [docs.stripe.com/testing](https://docs.stripe.com/testing).

The application synchronizes subscriptions through signed webhooks and verifies the provider subscription before granting matching PDF access. Exercise checkout, successful and failed renewals, cancellation, webhook retries, and account access before launch. Configure products and prices in Stripe Dashboard; the app does not create Stripe products or prices.

Resources: [Stripe Support](https://support.stripe.com) · [Stripe Docs](https://docs.stripe.com) · [Stripe MCP](https://docs.stripe.com/mcp)
