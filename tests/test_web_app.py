import os
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite://"

from web_app import (
    SUBSCRIPTION_PLANS,
    Subscription,
    User,
    app,
    calculate_property_analysis,
    db,
    paypal_api_request,
    stripe_api_request,
)


class PropertyAnalysisTests(unittest.TestCase):
    def test_rental_analysis_uses_refurbishment_cost_in_net_yield(self):
        result = calculate_property_analysis(
            purchase_price=200000,
            monthly_rent=1500,
            sq_ft=1000,
            refurb_cost=10000,
        )

        self.assertEqual(result["estimated_refurb"], 10000)
        self.assertEqual(result["total_capital_required"], 220000)
        self.assertEqual(result["gross_yield_percent"], 9.0)
        self.assertEqual(result["net_yield_percent"], 8.18)
        self.assertEqual(result["deal_rating"], "Gold")

    def test_hmo_calculates_rent_across_rooms(self):
        result = calculate_property_analysis(
            purchase_price=250000,
            monthly_rent=600,
            sq_ft=1400,
            property_type="hmo",
            refurb_cost=20000,
            bedrooms=5,
        )

        self.assertEqual(result["annual_income"], 36000)
        self.assertEqual(result["gross_yield_percent"], 14.4)

    def test_flip_uses_after_repair_value_for_profit(self):
        result = calculate_property_analysis(
            purchase_price=200000,
            monthly_rent=0,
            sq_ft=1000,
            property_type="flip",
            refurb_cost=20000,
            arv=260000,
        )

        self.assertEqual(result["headline_label"], "Estimated flip profit")
        self.assertEqual(result["headline_value"], 30000)
        self.assertEqual(result["deal_rating"], "Silver")

    def test_invalid_property_type_and_non_finite_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "valid property type"):
            calculate_property_analysis(200000, 1500, 1000, property_type="unknown")
        with self.assertRaisesRegex(ValueError, "valid number"):
            calculate_property_analysis(float("inf"), 1500, 1000)


class WebAppTests(unittest.TestCase):
    def setUp(self):
        app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            SECRET_KEY="test-session-key",
            SESSION_SECRET_CONFIGURED=True,
            PAYPAL_CLIENT_ID="client-id",
            PAYPAL_CLIENT_SECRET="client-secret",
            PAYPAL_WEBHOOK_ID="WH-test",
            PAYPAL_ENV="sandbox",
            PAYPAL_CURRENCY="GBP",
            STRIPE_SECRET_KEY="sk_test_checkout",
            STRIPE_WEBHOOK_SECRET="whsec_test",
            STRIPE_FOUNDERS_COUPON_ID="",
            PUBLIC_BASE_URL="https://wholesale-scout.example",
        )
        for property_type, plan in SUBSCRIPTION_PLANS.items():
            plan["stripe_price_id"] = f"price_{property_type}"
            plan["paypal_plan_id"] = f"P-{property_type}"
        with app.app_context():
            Subscription.query.delete()
            User.query.delete()
            db.session.commit()
        self.client = app.test_client()
        registration = self.client.post(
            "/register",
            data={"email": "customer@example.com", "password": "correct-horse-battery"},
        )
        self.assertEqual(registration.status_code, 302)
        with app.app_context():
            self.user_id = User.query.filter_by(email="customer@example.com").one().id

    def test_home_and_pdf_studio_render(self):
        home = self.client.get("/")
        studio = self.client.get("/pdf-studio?property_type=hmo")

        self.assertEqual(home.status_code, 200)
        self.assertIn(b"Try an example", home.data)
        self.assertIn(b"Starter rental", home.data)
        self.assertEqual(studio.status_code, 200)
        self.assertIn(b"Make the report", studio.data)
        self.assertIn(b"\xc2\xa38.99", studio.data)
        self.assertIn(b"\xc2\xa312.99", studio.data)
        self.assertIn(b"\xc2\xa316.99", studio.data)
        self.assertIn(b"PayPal", studio.data)
        self.assertIn(b"Subscribe by card", studio.data)
        self.assertIn(b"Ask Scout", home.data)
        self.assertIn(b"Try an example", home.data)

    def test_customer_account_and_subscription_survive_sign_out_and_sign_in(self):
        with app.app_context():
            db.session.add(Subscription(
                user_id=self.user_id,
                provider="stripe",
                provider_subscription_id="sub_persistent",
                property_type="apartment_rental",
                status="active",
            ))
            db.session.commit()

        logout_response = self.client.post("/logout")
        self.assertEqual(logout_response.status_code, 302)
        new_device = app.test_client()
        login_response = new_device.post(
            "/login",
            data={"email": "customer@example.com", "password": "correct-horse-battery"},
        )
        self.assertEqual(login_response.status_code, 302)
        account_response = new_device.get("/account")

        self.assertEqual(account_response.status_code, 200)
        self.assertIn(b"Stripe", account_response.data)
        self.assertIn(b"Apartment rental", account_response.data)
        download = new_device.post(
            "/pdf-studio",
            data={
                "action": "download_pdf",
                "property_type": "apartment_rental",
                "purchase_price": "200000",
                "monthly_rent": "1200",
                "sq_ft": "900",
            },
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.mimetype, "application/pdf")

    def test_new_account_password_is_hashed_and_duplicate_email_is_rejected(self):
        with app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertNotEqual(user.password_hash, "correct-horse-battery")
            self.assertTrue(user.password_hash.startswith("scrypt:"))
        duplicate = app.test_client().post(
            "/register",
            data={"email": "CUSTOMER@example.com", "password": "another-secure-password"},
            follow_redirects=True,
        )
        self.assertIn(b"account with that email already exists", duplicate.data)

    def test_subscription_checkout_requires_a_customer_account(self):
        anonymous = app.test_client()

        response = anonymous.post("/checkout/card/hmo")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_account_mutations_require_csrf_tokens(self):
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            response = self.client.post("/logout")
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

        self.assertEqual(response.status_code, 400)

    def test_checkout_and_pdf_studio_render_without_deployment_secrets(self):
        app.config["SESSION_SECRET_CONFIGURED"] = False

        studio = self.client.get("/pdf-studio")
        card_response = self.client.post("/checkout/card/hmo", follow_redirects=True)

        self.assertEqual(studio.status_code, 200)
        self.assertEqual(card_response.status_code, 200)
        self.assertIn(b"Card subscriptions are not configured", card_response.data)

    def test_homepage_generates_a_fillable_property_pdf(self):
        response = self.client.post(
            "/",
            data={
                "action": "download_pdf",
                "property_type": "hmo",
                "purchase_price": "250000",
                "monthly_rent": "600",
                "bedrooms": "5",
                "sq_ft": "1400",
                "refurb_cost": "20000",
                "arv": "0",
                "summary": "Five room property",
            },
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/pdf-studio?property_type=hmo#plans", response.headers["Location"])

    def test_pdf_studio_generates_a_download_for_each_property_type(self):
        for property_type in (
            "single_family_rental",
            "apartment_rental",
            "hmo",
            "flip",
            "commercial",
        ):
            with self.subTest(property_type=property_type):
                response = self.client.post(
                    "/pdf-studio",
                    data={
                        "action": "download_pdf",
                        "property_type": property_type,
                        "purchase_price": "200000",
                        "monthly_rent": "1500",
                        "bedrooms": "5",
                        "sq_ft": "1000",
                        "refurb_cost": "10000",
                        "arv": "260000",
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertIn(b"active monthly", response.data)
                self.assertIn(b"Download editable PDF", response.data)

    def test_matching_active_subscription_unlocks_editable_pdf(self):
        with app.app_context():
            db.session.add(Subscription(
                user_id=self.user_id,
                provider="stripe",
                provider_subscription_id="sub_hmo",
                property_type="hmo",
                status="active",
            ))
            db.session.commit()

        response = self.client.post(
            "/pdf-studio",
            data={
                "action": "download_pdf",
                "property_type": "hmo",
                "purchase_price": "250000",
                "monthly_rent": "600",
                "bedrooms": "5",
                "sq_ft": "1400",
                "refurb_cost": "20000",
                "arv": "0",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))
        self.assertIn(b"/AcroForm", response.data)

    def test_sample_property_pdfs_require_matching_active_subscription(self):
        anonymous = app.test_client()
        response = anonymous.get("/pdf-studio/sample/hmo")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        with app.app_context():
            db.session.add(Subscription(
                user_id=self.user_id,
                provider="stripe",
                provider_subscription_id="sub_apartment_sample",
                property_type="apartment_rental",
                status="active",
            ))
            db.session.commit()

        denied = self.client.get(
            "/pdf-studio/sample/hmo",
            follow_redirects=True,
        )
        self.assertEqual(denied.status_code, 200)
        self.assertIn(b"An active monthly HMO / multi-let subscription is required", denied.data)

    def test_each_matching_active_subscription_unlocks_its_sample_pdf(self):
        property_types = (
            "single_family_rental",
            "apartment_rental",
            "hmo",
            "flip",
            "commercial",
        )
        with app.app_context():
            for property_type in property_types:
                db.session.add(Subscription(
                    user_id=self.user_id,
                    provider="stripe",
                    provider_subscription_id=f"sub_sample_{property_type}",
                    property_type=property_type,
                    status="active",
                ))
            db.session.commit()

        for property_type in property_types:
            with self.subTest(property_type=property_type):
                response = self.client.get(f"/pdf-studio/sample/{property_type}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "application/pdf")
                self.assertTrue(response.data.startswith(b"%PDF"))
                self.assertIn(b"/AcroForm", response.data)
                self.assertIn("Wholesale_Scout_Sample_", response.headers["Content-Disposition"])

        studio = self.client.get("/pdf-studio")
        self.assertIn(b"Download sample PDF", studio.data)

    def test_paypal_is_disabled_without_server_configuration(self):
        app.config["PAYPAL_CLIENT_SECRET"] = ""

        response = self.client.get("/api/paypal/config")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["available"])
        self.assertEqual(response.get_json()["client_id"], "")
        self.assertEqual(response.get_json()["plan_ids"], {})

    def test_card_checkout_requires_complete_stripe_configuration(self):
        app.config["STRIPE_SECRET_KEY"] = ""

        response = self.client.post("/checkout/card/hmo", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Card subscriptions are not configured", response.data)

    def test_live_stripe_checkout_requires_https(self):
        app.config["STRIPE_SECRET_KEY"] = "sk_live_private"
        app.config["PUBLIC_BASE_URL"] = "http://wholesale-scout.example"

        response = self.client.post("/checkout/card/hmo", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Card subscriptions are not configured", response.data)

    @patch("web_app.stripe_api_request")
    def test_card_checkout_redirects_to_stripes_hosted_payment_page(self, stripe_request):
        stripe_request.side_effect = [
            {
                "id": "price_hmo",
                "active": True,
                "currency": "gbp",
                "unit_amount": 1299,
                "recurring": {"interval": "month", "interval_count": 1},
            },
            {"id": "cs_test_123", "url": "https://checkout.stripe.com/c/pay/cs_test_123"},
        ]

        response = self.client.post("/checkout/card/hmo")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["Location"],
            "https://checkout.stripe.com/c/pay/cs_test_123",
        )
        payload = stripe_request.call_args_list[1].args[3]
        self.assertEqual(payload["mode"], "subscription")
        self.assertEqual(payload["ui_mode"], "hosted_page")
        self.assertEqual(payload["billing_address_collection"], "auto")
        self.assertEqual(payload["payment_method_collection"], "always")
        self.assertEqual(payload["allow_promotion_codes"], "false")
        self.assertEqual(payload["submit_type"], "auto")
        self.assertEqual(payload["integration_identifier"], "hosted_web_0002")
        self.assertEqual(payload["origin_context"], "web")
        self.assertNotIn("phone_number_collection[enabled]", payload)
        self.assertNotIn("automatic_tax[enabled]", payload)
        self.assertEqual(payload["line_items[0][price]"], "price_hmo")
        self.assertEqual(payload["line_items[0][quantity]"], "1")
        self.assertEqual(payload["client_reference_id"], self.user_id)
        self.assertEqual(payload["subscription_data[metadata][user_id]"], self.user_id)
        self.assertIn("{CHECKOUT_SESSION_ID}", payload["success_url"])
        self.assertTrue(payload["cancel_url"].endswith("/pdf-studio?card_checkout=cancelled&property_type=hmo#plans"))

    @patch("web_app.stripe_api_request")
    def test_stripe_success_page_is_shown_only_after_active_subscription_is_verified(self, stripe_request):
        stripe_request.side_effect = [
            {
                "id": "cs_test_123",
                "mode": "subscription",
                "status": "complete",
                "client_reference_id": self.user_id,
                "line_items": {
                    "data": [{
                        "price": {
                            "id": "price_hmo",
                            "recurring": {"interval": "month"},
                        },
                        "quantity": 1,
                    }],
                },
                "subscription": {
                    "id": "sub_hmo",
                    "status": "active",
                    "metadata": {"user_id": self.user_id, "property_type": "hmo"},
                    "items": {
                        "data": [{
                            "price": {
                                "id": "price_hmo",
                            },
                        }],
                    },
                },
            },
        ]

        response = self.client.get(
            "/checkout/card/success?session_id=cs_test_123&property_type=hmo"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Thanks for subscribing", response.data)
        self.assertIn(b"Stripe confirmed your active HMO / multi-let subscription", response.data)
        with app.app_context():
            saved = Subscription.query.filter_by(
                provider="stripe",
                provider_subscription_id="sub_hmo",
            ).one()
            self.assertEqual(saved.user_id, self.user_id)
            self.assertEqual(saved.status, "active")

    @patch("web_app.stripe_api_request")
    def test_stripe_success_page_does_not_claim_success_for_incomplete_checkout(self, stripe_request):
        stripe_request.return_value = {
            "id": "cs_test_123",
            "mode": "subscription",
            "status": "open",
            "client_reference_id": self.user_id,
            "line_items": {"data": []},
            "subscription": None,
        }

        response = self.client.get(
            "/checkout/card/success?session_id=cs_test_123&property_type=hmo",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Thanks for subscribing", response.data)
        self.assertIn(b"Stripe has not confirmed an active monthly subscription", response.data)

    @patch("web_app.stripe_api_request")
    def test_card_checkout_applies_only_the_configured_limited_founders_coupon(self, stripe_request):
        app.config["STRIPE_FOUNDERS_COUPON_ID"] = "founders25"
        stripe_request.side_effect = [
            {
                "id": "price_hmo",
                "active": True,
                "currency": "gbp",
                "unit_amount": 1299,
                "recurring": {"interval": "month", "interval_count": 1},
            },
            {
                "id": "founders25",
                "valid": True,
                "percent_off": 25,
                "duration": "repeating",
                "duration_in_months": 3,
                "max_redemptions": 10,
                "times_redeemed": 9,
            },
            {"id": "cs_test_123", "url": "https://checkout.stripe.com/c/pay/cs_test_123"},
        ]

        response = self.client.post("/checkout/card/hmo")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(stripe_request.call_args_list[1].args[2], "/v1/coupons/founders25")
        checkout_payload = stripe_request.call_args_list[2].args[3]
        self.assertEqual(checkout_payload["discounts[0][coupon]"], "founders25")

    @patch("web_app.stripe_api_request")
    def test_card_checkout_does_not_advertise_or_apply_coupon_with_no_redemption_cap(self, stripe_request):
        app.config["STRIPE_FOUNDERS_COUPON_ID"] = "uncapped"
        stripe_request.side_effect = [
            {
                "id": "price_hmo",
                "active": True,
                "currency": "gbp",
                "unit_amount": 1299,
                "recurring": {"interval": "month", "interval_count": 1},
            },
            {
                "id": "uncapped",
                "valid": True,
                "percent_off": 25,
                "duration": "repeating",
                "duration_in_months": 3,
                "max_redemptions": None,
                "times_redeemed": 0,
            },
            {
                "id": "uncapped",
                "valid": True,
                "percent_off": 25,
                "duration": "repeating",
                "duration_in_months": 3,
                "max_redemptions": None,
                "times_redeemed": 0,
            },
        ]

        response = self.client.post("/checkout/card/hmo", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"configuration needs attention", response.data)
        self.assertEqual(stripe_request.call_count, 3)

    @patch("web_app.stripe_api_request")
    def test_invalid_coupon_is_not_advertised_on_the_pricing_page(self, stripe_request):
        app.config["STRIPE_FOUNDERS_COUPON_ID"] = "uncapped"
        stripe_request.return_value = {
            "id": "uncapped",
            "valid": True,
            "percent_off": 25,
            "duration": "repeating",
            "duration_in_months": 3,
            "max_redemptions": None,
            "times_redeemed": 0,
        }

        response = self.client.get("/pdf-studio")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Founding customer offer", response.data)

    @patch("web_app.stripe_api_request")
    def test_card_checkout_refuses_non_stripe_redirect_urls(self, stripe_request):
        stripe_request.side_effect = [
            {
                "id": "price_hmo",
                "active": True,
                "currency": "gbp",
                "unit_amount": 1299,
                "recurring": {"interval": "month", "interval_count": 1},
            },
            {"id": "cs_test_123", "url": "https://checkout.attacker.example/pay"},
        ]

        response = self.client.post("/checkout/card/hmo")

        self.assertEqual(response.status_code, 303)
        self.assertIn("/pdf-studio#plans", response.headers["Location"])
        self.assertNotIn("attacker.example", response.headers["Location"])

    @patch("web_app.stripe_api_request")
    def test_active_stripe_subscription_is_verified_before_access(self, stripe_request):
        stripe_request.return_value = {
            "id": "cs_test_123",
            "mode": "subscription",
            "status": "complete",
            "client_reference_id": self.user_id,
            "line_items": {
                "data": [{
                    "price": {
                        "id": "price_hmo",
                        "recurring": {"interval": "month"},
                    },
                    "quantity": 1,
                }],
            },
            "subscription": {
                "id": "sub_123",
                "status": "active",
                "metadata": {"user_id": self.user_id, "property_type": "hmo"},
                "customer": "cus_123",
                "current_period_end": 1792531200,
                "items": {"data": [{"price": {"id": "price_hmo"}}]},
            },
        }

        response = self.client.get(
            "/checkout/card/success?session_id=cs_test_123&property_type=hmo",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Thanks for subscribing", response.data)
        self.assertIn(b"Stripe confirmed your active HMO / multi-let subscription", response.data)
        with app.app_context():
            record = Subscription.query.filter_by(provider_subscription_id="sub_123").one()
            self.assertEqual(record.user_id, self.user_id)
            self.assertEqual(record.status, "active")
            self.assertEqual(record.current_period_end, 1792531200)
            self.assertEqual(db.session.get(User, self.user_id).stripe_customer_id, "cus_123")

    @patch("web_app.stripe_api_request")
    def test_wrong_price_stripe_subscription_is_not_confirmed(self, stripe_request):
        stripe_request.return_value = {
            "id": "cs_test_456",
            "mode": "subscription",
            "status": "complete",
            "line_items": {
                "data": [{
                    "price": {
                        "id": "price_other",
                        "recurring": {"interval": "month"},
                    },
                    "quantity": 1,
                }],
            },
            "subscription": {
                "id": "sub_456",
                "status": "active",
                "metadata": {"user_id": self.user_id, "property_type": "hmo"},
                "items": {"data": [{"price": {"id": "price_other"}}]},
            },
        }

        response = self.client.get(
            "/checkout/card/success?session_id=cs_test_456&property_type=hmo",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Stripe has not confirmed an active monthly subscription", response.data)
        with app.app_context():
            self.assertIsNone(Subscription.query.filter_by(provider_subscription_id="sub_456").one_or_none())

    @patch("web_app.stripe_api_request")
    def test_stripe_webhook_signature_updates_persistent_subscription(self, stripe_request):
        app.config["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
        stripe_request.return_value = {
            "id": "sub_webhook",
            "status": "active",
            "metadata": {"user_id": self.user_id, "property_type": "hmo"},
            "customer": "cus_webhook",
            "current_period_end": 1792531200,
            "items": {"data": [{"price": {"id": "price_hmo"}}]},
        }
        event = {
            "id": "evt_test",
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_webhook"}},
        }
        payload = json.dumps(event).encode()
        timestamp = int(time.time())
        signed = f"{timestamp}.".encode() + payload
        signature = hmac.new(b"whsec_test", signed, hashlib.sha256).hexdigest()

        response = self.client.post(
            "/webhooks/stripe",
            data=payload,
            content_type="application/json",
            headers={"Stripe-Signature": f"t={timestamp},v1={signature}"},
        )

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            record = Subscription.query.filter_by(provider_subscription_id="sub_webhook").one()
            self.assertEqual(record.status, "active")
            self.assertEqual(record.user_id, self.user_id)
            self.assertEqual(db.session.get(User, self.user_id).stripe_customer_id, "cus_webhook")

    def test_stripe_webhook_rejects_invalid_signature(self):
        app.config["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
        response = self.client.post(
            "/webhooks/stripe",
            data=b'{"type":"customer.subscription.updated","data":{"object":{"id":"sub_bad"}}}',
            content_type="application/json",
            headers={"Stripe-Signature": "t=1,v1=invalid"},
        )

        self.assertEqual(response.status_code, 400)

    @patch("web_app.stripe_api_request")
    def test_customer_can_open_stripe_billing_portal(self, stripe_request):
        stripe_request.return_value = {"url": "https://billing.stripe.com/p/session/test"}
        with app.app_context():
            user = db.session.get(User, self.user_id)
            user.stripe_customer_id = "cus_123"
            db.session.commit()

        response = self.client.post("/billing/stripe/portal")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "https://billing.stripe.com/p/session/test")
        self.assertEqual(
            stripe_request.call_args.args[3]["customer"],
            "cus_123",
        )

    @patch("web_app._paypal_plan_matches_price", return_value=True)
    def test_paypal_config_exposes_only_public_checkout_details(self, _):
        response = self.client.get("/api/paypal/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "available": True,
                "client_id": "client-id",
                "plan_ids": {
                    key: f"P-{key}" for key in SUBSCRIPTION_PLANS
                },
                "currency": "GBP",
                "account_id": self.user_id,
            },
        )
        self.assertNotIn("client-secret", response.get_data(as_text=True))

    def test_paypal_verification_rejects_non_object_json(self):
        response = self.client.post(
            "/api/paypal/subscriptions/verify",
            json=None,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON subscription request", response.get_json()["error"])

    @patch("web_app.paypal_api_request")
    def test_verified_paypal_webhook_updates_persistent_account_state(self, paypal_request):
        paypal_request.side_effect = [
            {"verification_status": "SUCCESS"},
            {
                "id": "I-webhook",
                "status": "ACTIVE",
                "plan_id": "P-hmo",
                "custom_id": self.user_id,
                "subscriber": {"email_address": "customer@example.com"},
            },
            {
                "id": "P-hmo",
                "status": "ACTIVE",
                "billing_cycles": [{
                    "tenure_type": "REGULAR",
                    "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                    "pricing_scheme": {"fixed_price": {"currency_code": "GBP", "value": "12.99"}},
                }],
            },
        ]
        event = {
            "id": "WH-EVENT",
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {"id": "I-webhook"},
        }

        response = self.client.post(
            "/webhooks/paypal",
            json=event,
            headers={
                "PAYPAL-AUTH-ALGO": "SHA256withRSA",
                "PAYPAL-CERT-URL": "https://api.paypal.com/cert",
                "PAYPAL-TRANSMISSION-ID": "transmission-id",
                "PAYPAL-TRANSMISSION-SIG": "signature",
                "PAYPAL-TRANSMISSION-TIME": "2026-10-20T22:00:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(paypal_request.call_args_list[0].args[2], "/v1/notifications/verify-webhook-signature")
        with app.app_context():
            record = Subscription.query.filter_by(provider_subscription_id="I-webhook").one()
            self.assertEqual(record.user_id, self.user_id)
            self.assertEqual(record.status, "ACTIVE")

    @patch("web_app.paypal_api_request")
    def test_paypal_subscription_is_reported_only_after_server_verification(self, paypal_request):
        paypal_request.side_effect = [
            {
                "id": "I-123",
                "status": "ACTIVE",
                "plan_id": "P-hmo",
                "custom_id": self.user_id,
                "subscriber": {"email_address": "customer@example.com"},
            },
            {
                "id": "P-hmo",
                "status": "ACTIVE",
                "billing_cycles": [{
                    "tenure_type": "REGULAR",
                    "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                    "pricing_scheme": {"fixed_price": {"currency_code": "GBP", "value": "12.99"}},
                }],
            },
        ]

        response = self.client.post(
            "/api/paypal/subscriptions/verify",
            json={"property_type": "hmo", "subscription_id": "I-123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ACTIVE")
        with app.app_context():
            record = Subscription.query.filter_by(provider_subscription_id="I-123").one()
            self.assertEqual(record.user_id, self.user_id)

    @patch("web_app.paypal_api_request")
    def test_paypal_does_not_confirm_inactive_or_wrong_plan(self, paypal_request):
        paypal_request.return_value = {
            "id": "I-456",
            "status": "SUSPENDED",
            "plan_id": "P-other",
        }

        response = self.client.post(
            "/api/paypal/subscriptions/verify",
            json={"property_type": "hmo", "subscription_id": "I-456"},
        )

        self.assertEqual(response.status_code, 402)
        self.assertNotEqual(response.get_json().get("status"), "ACTIVE")

    @patch("web_app.paypal_api_request")
    def test_paypal_does_not_unlock_for_a_mispriced_monthly_plan(self, paypal_request):
        paypal_request.side_effect = [
            {"id": "I-789", "status": "ACTIVE", "plan_id": "P-hmo"},
            {
                "id": "P-hmo",
                "status": "ACTIVE",
                "billing_cycles": [{
                    "tenure_type": "REGULAR",
                    "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                    "pricing_scheme": {"fixed_price": {"currency_code": "GBP", "value": "1.00"}},
                }],
            },
        ]

        response = self.client.post(
            "/api/paypal/subscriptions/verify",
            json={"property_type": "hmo", "subscription_id": "I-789"},
        )

        self.assertEqual(response.status_code, 402)
        self.assertNotEqual(response.get_json().get("status"), "ACTIVE")
        with app.app_context():
            self.assertIsNone(Subscription.query.filter_by(provider_subscription_id="I-789").one_or_none())

    @patch("web_app.paypal_api_request")
    def test_customer_can_cancel_owned_paypal_subscription(self, paypal_request):
        with app.app_context():
            subscription = Subscription(
                user_id=self.user_id,
                provider="paypal",
                provider_subscription_id="I-cancel-me",
                property_type="hmo",
                status="ACTIVE",
            )
            db.session.add(subscription)
            db.session.commit()
            row_id = subscription.id
        paypal_request.return_value = {}

        response = self.client.post(f"/billing/paypal/{row_id}/cancel")

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Subscription, row_id).status, "CANCELLED")

    def test_customer_cannot_cancel_another_accounts_paypal_subscription(self):
        with app.app_context():
            other_user = User(
                email="other@example.com",
                password_hash="not-used",
            )
            db.session.add(other_user)
            db.session.flush()
            subscription = Subscription(
                user_id=other_user.id,
                provider="paypal",
                provider_subscription_id="I-not-yours",
                property_type="hmo",
                status="ACTIVE",
            )
            db.session.add(subscription)
            db.session.commit()
            row_id = subscription.id

        response = self.client.post(f"/billing/paypal/{row_id}/cancel")

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Subscription, row_id).status, "ACTIVE")

    @patch("web_app.urllib.request.urlopen")
    def test_stripe_api_request_sends_the_secret_only_in_server_auth(self, urlopen):
        stripe_response = MagicMock()
        stripe_response.__enter__.return_value.read.return_value = b'{"id":"cs_test_123"}'
        urlopen.return_value = stripe_response

        result = stripe_api_request(
            {"secret_key": "sk_test_private"},
            "POST",
            "/v1/checkout/sessions",
            {"mode": "payment", "line_items[0][price]": "price_report"},
        )

        self.assertEqual(result["id"], "cs_test_123")
        checkout_request = urlopen.call_args.args[0]
        self.assertTrue(checkout_request.get_header("Authorization").startswith("Basic "))
        self.assertIn(b"mode=payment", checkout_request.data)
        self.assertIn(b"line_items%5B0%5D%5Bprice%5D=price_report", checkout_request.data)

    @patch("web_app.urllib.request.urlopen")
    def test_paypal_rest_request_uses_the_oauth_access_token(self, urlopen):
        token_response = MagicMock()
        token_response.__enter__.return_value.read.return_value = b'{"access_token":"test-token"}'
        api_response = MagicMock()
        api_response.__enter__.return_value.read.return_value = b'{"id":"order-789"}'
        urlopen.side_effect = [token_response, api_response]
        settings = {
            "base_url": "https://api-m.sandbox.paypal.com",
            "client_id": "client-id",
            "client_secret": "client-secret",
        }

        result = paypal_api_request(settings, "POST", "/v2/checkout/orders", {})

        self.assertEqual(result["id"], "order-789")
        api_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(api_request.get_header("Authorization"), "Bearer test-token")


if __name__ == "__main__":
    unittest.main()
