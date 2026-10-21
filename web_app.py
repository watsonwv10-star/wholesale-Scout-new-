import io
import json
import math
import os
import secrets
import tempfile
import base64
import hashlib
import hmac
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import UniqueConstraint
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from app import analyze_property_photo, calculate_property_yield


app = Flask(__name__)
database_url = os.getenv("DATABASE_URL", "").strip()
if database_url.startswith("postgres://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
elif database_url.startswith("postgresql://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
if os.getenv("RENDER") and not database_url:
    raise RuntimeError("Set DATABASE_URL to persistent PostgreSQL before deploying customer accounts.")
app.config.update(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY") or secrets.token_urlsafe(32),
    SESSION_SECRET_CONFIGURED=bool(os.getenv("FLASK_SECRET_KEY")),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        os.getenv("RENDER", "").lower() == "true"
        or os.getenv("PAYPAL_ENV", "sandbox").lower() == "live"
        or os.getenv("STRIPE_SECRET_KEY", "").startswith("sk_live_")
    ),
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SECURE=(
        os.getenv("RENDER", "").lower() == "true"
        or os.getenv("PAYPAL_ENV", "sandbox").lower() == "live"
        or os.getenv("STRIPE_SECRET_KEY", "").startswith("sk_live_")
    ),
    SQLALCHEMY_DATABASE_URI=database_url or "sqlite:///wholesale_scout.sqlite3",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    WTF_CSRF_TIME_LIMIT=3600,
    PAYPAL_CLIENT_ID=os.getenv("PAYPAL_CLIENT_ID", ""),
    PAYPAL_CLIENT_SECRET=os.getenv("PAYPAL_CLIENT_SECRET", ""),
    PAYPAL_MERCHANT_ID=os.getenv("PAYPAL_MERCHANT_ID", ""),
    PAYPAL_ENV=os.getenv("PAYPAL_ENV", "sandbox").lower(),
    PAYPAL_CURRENCY=os.getenv("PAYPAL_CURRENCY", "GBP").upper(),
    PAYPAL_WEBHOOK_ID=os.getenv("PAYPAL_WEBHOOK_ID", ""),
    STRIPE_SECRET_KEY=os.getenv("STRIPE_SECRET_KEY", ""),
    STRIPE_WEBHOOK_SECRET=os.getenv("STRIPE_WEBHOOK_SECRET", ""),
    STRIPE_FOUNDERS_COUPON_ID=os.getenv("STRIPE_FOUNDERS_COUPON_ID", ""),
    PUBLIC_BASE_URL=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Sign in to manage your subscription and downloads."


class User(UserMixin, db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    stripe_customer_id = db.Column(db.String(255), unique=True)
    subscriptions = db.relationship("Subscription", back_populates="user", cascade="all, delete-orphan")


class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(36), db.ForeignKey("user.id"), nullable=False, index=True)
    provider = db.Column(db.String(16), nullable=False)
    provider_subscription_id = db.Column(db.String(255), nullable=False)
    property_type = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(32), nullable=False)
    current_period_end = db.Column(db.Integer)
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now(), onupdate=db.func.now())
    user = db.relationship("User", back_populates="subscriptions")
    __table_args__ = (
        UniqueConstraint("provider", "provider_subscription_id", name="uq_provider_subscription"),
    )


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, user_id)


@app.template_filter("timestamp_date")
def format_timestamp_date(value):
    if not isinstance(value, int):
        return ""
    return time.strftime("%d %b %Y", time.gmtime(value))


with app.app_context():
    db.create_all()

PROPERTY_TYPES = {
    "single_family_rental": {
        "label": "Single-family rental",
        "accent": colors.HexColor("#177e68"),
        "subtitle": "Rental yield and cash-flow snapshot",
    },
    "apartment_rental": {
        "label": "Apartment rental",
        "accent": colors.HexColor("#2563a6"),
        "subtitle": "Apartment income and yield snapshot",
    },
    "hmo": {
        "label": "HMO / multi-let",
        "accent": colors.HexColor("#7753ad"),
        "subtitle": "Room-by-room rental income snapshot",
    },
    "flip": {
        "label": "Renovation / flip",
        "accent": colors.HexColor("#ca6b37"),
        "subtitle": "After-repair value and resale snapshot",
    },
    "commercial": {
        "label": "Commercial property",
        "accent": colors.HexColor("#375b76"),
        "subtitle": "Commercial rental yield snapshot",
    },
}

SUBSCRIPTION_PLANS = {
    "single_family_rental": {
        "label": "Single-family rental",
        "monthly_price": Decimal("8.99"),
        "stripe_price_id": os.getenv("STRIPE_PRICE_SINGLE_FAMILY_RENTAL", ""),
        "paypal_plan_id": os.getenv("PAYPAL_PLAN_SINGLE_FAMILY_RENTAL", ""),
    },
    "apartment_rental": {
        "label": "Apartment rental",
        "monthly_price": Decimal("8.99"),
        "stripe_price_id": os.getenv("STRIPE_PRICE_APARTMENT_RENTAL", ""),
        "paypal_plan_id": os.getenv("PAYPAL_PLAN_APARTMENT_RENTAL", ""),
    },
    "hmo": {
        "label": "HMO / multi-let",
        "monthly_price": Decimal("12.99"),
        "stripe_price_id": os.getenv("STRIPE_PRICE_HMO", ""),
        "paypal_plan_id": os.getenv("PAYPAL_PLAN_HMO", ""),
    },
    "flip": {
        "label": "Renovation / flip",
        "monthly_price": Decimal("12.99"),
        "stripe_price_id": os.getenv("STRIPE_PRICE_FLIP", ""),
        "paypal_plan_id": os.getenv("PAYPAL_PLAN_FLIP", ""),
    },
    "commercial": {
        "label": "Commercial property",
        "monthly_price": Decimal("16.99"),
        "stripe_price_id": os.getenv("STRIPE_PRICE_COMMERCIAL", ""),
        "paypal_plan_id": os.getenv("PAYPAL_PLAN_COMMERCIAL", ""),
    },
}

SAMPLE_PROPERTY_REPORTS = {
    "single_family_rental": {
        "filename": "Wholesale_Scout_Sample_Single_Family_Rental.pdf",
        "property_address": "Illustrative sample - Bristol",
        "purchase_price": "185000",
        "monthly_rent": "1200",
        "sq_ft": "1000",
        "bedrooms": "2",
        "refurb_cost": "8000",
        "arv": "0",
        "summary": "Example single-family rental. Figures are illustrative only.",
    },
    "apartment_rental": {
        "filename": "Wholesale_Scout_Sample_Apartment_Rental.pdf",
        "property_address": "Illustrative sample - Leeds",
        "purchase_price": "150000",
        "monthly_rent": "950",
        "sq_ft": "720",
        "bedrooms": "2",
        "refurb_cost": "5000",
        "arv": "0",
        "summary": "Example apartment rental. Figures are illustrative only.",
    },
    "hmo": {
        "filename": "Wholesale_Scout_Sample_HMO_Multi_Let.pdf",
        "property_address": "Illustrative sample - Manchester",
        "purchase_price": "250000",
        "monthly_rent": "600",
        "sq_ft": "1400",
        "bedrooms": "5",
        "refurb_cost": "20000",
        "arv": "0",
        "summary": "Example five-room HMO; rent is per room. Figures are illustrative only.",
    },
    "flip": {
        "filename": "Wholesale_Scout_Sample_Renovation_Flip.pdf",
        "property_address": "Illustrative sample - Birmingham",
        "purchase_price": "200000",
        "monthly_rent": "0",
        "sq_ft": "1200",
        "bedrooms": "2",
        "refurb_cost": "30000",
        "arv": "300000",
        "summary": "Example renovation project. After-repair value and costs are illustrative only.",
    },
    "commercial": {
        "filename": "Wholesale_Scout_Sample_Commercial_Property.pdf",
        "property_address": "Illustrative sample - Sheffield",
        "purchase_price": "325000",
        "monthly_rent": "2600",
        "sq_ft": "2400",
        "bedrooms": "2",
        "refurb_cost": "15000",
        "arv": "0",
        "summary": "Example commercial rental. Figures are illustrative only.",
    },
}

PAYPAL_API_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


def calculate_property_analysis(
    purchase_price,
    monthly_rent,
    sq_ft,
    property_type="single_family_rental",
    refurb_cost=None,
    arv=0,
    bedrooms=1,
    condition_summary="",
):
    """Calculate a rental or flip summary for the homepage and PDF reports."""
    if property_type not in PROPERTY_TYPES:
        raise ValueError("Choose a valid property type.")

    values = {
        "purchase price": purchase_price,
        "monthly rent": monthly_rent,
        "floor area": sq_ft,
        "after-repair value": arv,
        "bedroom count": bedrooms,
    }
    for label, value in values.items():
        if not math.isfinite(value):
            raise ValueError(f"{label.capitalize()} must be a valid number.")
    if purchase_price <= 0 or sq_ft <= 0:
        raise ValueError("Purchase price and floor area must be greater than zero.")
    if monthly_rent < 0 or arv < 0 or bedrooms < 1:
        raise ValueError("Rent and after-repair value cannot be negative; bedrooms must be at least one.")
    if refurb_cost is not None and (not math.isfinite(refurb_cost) or refurb_cost < 0):
        raise ValueError("Refurbishment cost must be a valid, non-negative number.")

    estimated_refurb = calculate_property_yield(
        purchase_price, monthly_rent, sq_ft, condition_summary
    )["estimated_refurb"]
    actual_refurb = estimated_refurb if refurb_cost is None else refurb_cost
    total_capital = purchase_price + actual_refurb + purchase_price * 0.05
    rooms = bedrooms if property_type == "hmo" else 1
    annual_income = monthly_rent * rooms * 12

    result = {
        "property_type": property_type,
        "property_label": PROPERTY_TYPES[property_type]["label"],
        "estimated_refurb": round(actual_refurb, 2),
        "total_capital_required": round(total_capital, 2),
        "annual_income": round(annual_income, 2),
        "gross_yield_percent": round(annual_income / purchase_price * 100, 2),
        "net_yield_percent": round(annual_income / total_capital * 100, 2),
        "arv": round(arv, 2),
        "bedrooms": bedrooms,
        "purchase_price": round(purchase_price, 2),
        "monthly_rent": round(monthly_rent, 2),
        "sq_ft": round(sq_ft, 2),
        "condition_summary": condition_summary,
    }

    if property_type == "flip":
        profit = arv - total_capital
        roi = profit / total_capital * 100
        result.update(
            headline_label="Estimated flip profit",
            headline_value=round(profit, 2),
            secondary_label="Return on investment",
            secondary_value=round(roi, 2),
            deal_rating="Gold" if roi > 20 else "Silver" if roi > 10 else "Bronze",
        )
    else:
        result.update(
            headline_label="Net rental yield",
            headline_value=result["net_yield_percent"],
            secondary_label="Gross rental yield",
            secondary_value=result["gross_yield_percent"],
            deal_rating=(
                "Gold" if result["net_yield_percent"] > 8
                else "Silver" if result["net_yield_percent"] > 5
                else "Bronze"
            ),
        )
    return result


def _parse_property_form(form):
    """Parse and validate values shared by the calculator and PDF studio."""
    try:
        purchase_price = float(form.get("purchase_price", ""))
        monthly_rent = float(form.get("monthly_rent", "0") or 0)
        sq_ft = float(form.get("sq_ft", ""))
        bedrooms = int(form.get("bedrooms", "1") or 1)
        arv = float(form.get("arv", "0") or 0)
        refurb_raw = form.get("refurb_cost", "").strip()
        refurb_cost = float(refurb_raw) if refurb_raw else None
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter valid numbers for price, rent, area, bedrooms, and ARV.") from exc

    property_type = form.get("property_type", "single_family_rental")
    condition_summary = form.get("summary", "").strip()[:500]
    result = calculate_property_analysis(
        purchase_price=purchase_price,
        monthly_rent=monthly_rent,
        sq_ft=sq_ft,
        property_type=property_type,
        refurb_cost=refurb_cost,
        arv=arv,
        bedrooms=bedrooms,
        condition_summary=condition_summary,
    )
    result["property_address"] = form.get("property_address", "").strip()[:160]
    result["notes"] = form.get("notes", "").strip()[:1000]
    return result


def _safe_pdf_text(value, limit=160):
    text = str(value or "")
    return "".join(character for character in text if character.isprintable())[:limit]


def create_property_pdf(result):
    """Create a downloadable, fillable PDF report using the selected property layout."""
    output = io.BytesIO()
    page_width, page_height = letter
    pdf = canvas.Canvas(output, pagesize=letter)
    layout = PROPERTY_TYPES[result["property_type"]]
    accent = layout["accent"]
    dark = colors.HexColor("#142d3a")
    pale = colors.HexColor("#f3f7f6")

    pdf.setFillColor(dark)
    pdf.rect(0, page_height - 150, page_width, 150, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 23)
    pdf.drawString(48, page_height - 60, "WHOLESALE SCOUT")
    pdf.setFont("Helvetica-Bold", 17)
    pdf.drawString(48, page_height - 92, f"{layout['label']} deal report")
    pdf.setFont("Helvetica", 10)
    pdf.setFillColor(colors.HexColor("#d0dce0"))
    pdf.drawString(48, page_height - 115, layout["subtitle"])

    pdf.setFillColor(pale)
    pdf.roundRect(42, page_height - 245, page_width - 84, 70, 10, fill=1, stroke=0)
    pdf.setFillColor(accent)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(58, page_height - 198, result["headline_label"].upper())
    pdf.setFillColor(dark)
    pdf.setFont("Helvetica-Bold", 21)
    headline = f"£{result['headline_value']:,.2f}" if result["property_type"] == "flip" else f"{result['headline_value']:.2f}%"
    pdf.drawString(58, page_height - 228, headline)
    pdf.setFont("Helvetica", 10)
    pdf.drawRightString(
        page_width - 58,
        page_height - 214,
        f"{result['secondary_label']}: {result['secondary_value']:.2f}"
        + ("%" if result["property_type"] == "flip" else "%"),
    )

    pdf.setFillColor(dark)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(48, page_height - 278, "Your editable deal details")
    pdf.setFont("Helvetica", 8)
    pdf.setFillColor(colors.HexColor("#5e7078"))
    pdf.drawString(48, page_height - 294, "Select a field in a PDF reader to change its value.")

    fields = [
        ("Property address", "property_address", result.get("property_address", "")),
        ("Property type", "property_type", layout["label"]),
        ("Purchase price (£)", "purchase_price", f"{result['purchase_price']:,.2f}"),
        ("Monthly rent per unit (£)", "monthly_rent", f"{result['monthly_rent']:,.2f}"),
        ("Floor area (sq ft)", "floor_area", f"{result['sq_ft']:,.2f}"),
        ("Refurbishment budget (£)", "refurbishment", f"{result['estimated_refurb']:,.2f}"),
        ("After-repair value (£)", "after_repair_value", f"{result['arv']:,.2f}"),
        ("Bedrooms / rentable units", "bedrooms", str(result["bedrooms"])),
        ("Deal rating", "deal_rating", result["deal_rating"]),
    ]
    field_y = page_height - 337
    field_positions = [(48, field_y), (306, field_y)]
    for index, (label, name, value) in enumerate(fields):
        x, y = field_positions[index % 2]
        if index and index % 2 == 0:
            field_y -= 68
            field_positions = [(48, field_y), (306, field_y)]
            x, y = field_positions[0]
        pdf.setFillColor(dark)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(x, y + 38, label)
        pdf.acroForm.textfield(
            name=name,
            tooltip=label,
            x=x,
            y=y + 12,
            width=245,
            height=21,
            value=_safe_pdf_text(value, 120),
            fontName="Helvetica",
            fontSize=9,
            textColor=dark,
            fillColor=colors.white,
            borderColor=colors.HexColor("#d7e0e2"),
            borderWidth=1,
            forceBorder=True,
        )

    notes_y = field_y - 40
    pdf.setFillColor(dark)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(48, notes_y + 38, "Notes / condition summary")
    pdf.acroForm.textfield(
        name="notes",
        tooltip="Notes and property condition",
        x=48,
        y=notes_y - 35,
        width=516,
        height=64,
        value=_safe_pdf_text(result.get("notes") or result.get("condition_summary"), 400),
        fontName="Helvetica",
        fontSize=9,
        textColor=dark,
        fillColor=colors.white,
        borderColor=colors.HexColor("#d7e0e2"),
        borderWidth=1,
        forceBorder=True,
        fieldFlags="multiline",
    )

    pdf.setStrokeColor(colors.HexColor("#dce4e6"))
    pdf.line(48, 48, page_width - 48, 48)
    pdf.setFillColor(colors.HexColor("#687b82"))
    pdf.setFont("Helvetica", 8)
    pdf.drawString(48, 33, "Illustrative analysis only. Verify figures independently before investing.")
    pdf.drawRightString(page_width - 48, 33, f"Net yield: {result['net_yield_percent']:.2f}%")
    pdf.save()
    output.seek(0)
    return output


def _paypal_settings():
    """Return validated PayPal subscription configuration, or None if incomplete."""
    currency = str(app.config.get("PAYPAL_CURRENCY", "")).upper()
    environment = app.config.get("PAYPAL_ENV")
    if (
        not app.config.get("PAYPAL_CLIENT_ID")
        or not app.config.get("PAYPAL_CLIENT_SECRET")
        or not app.config.get("PAYPAL_WEBHOOK_ID")
        or not app.config.get("SESSION_SECRET_CONFIGURED")
        or environment not in PAYPAL_API_URLS
        or not currency.isalpha()
        or len(currency) != 3
    ):
        return None
    return {
        "client_id": app.config["PAYPAL_CLIENT_ID"],
        "client_secret": app.config["PAYPAL_CLIENT_SECRET"],
        "currency": currency,
        "base_url": PAYPAL_API_URLS[environment],
    }


class PayPalAPIError(Exception):
    """A PayPal API failure with a safe message for the browser."""


def _stripe_settings():
    """Return server-side Stripe subscription settings, or None if incomplete."""
    secret_key = str(app.config.get("STRIPE_SECRET_KEY", "")).strip()
    webhook_secret = str(app.config.get("STRIPE_WEBHOOK_SECRET", "")).strip()
    public_base_url = str(app.config.get("PUBLIC_BASE_URL", "")).rstrip("/")
    parsed_base_url = urllib.parse.urlsplit(public_base_url)
    is_test_key = secret_key.startswith("sk_test_")
    is_live_key = secret_key.startswith("sk_live_")
    if (
        not (is_test_key or is_live_key)
        or not webhook_secret
        or not app.config.get("SESSION_SECRET_CONFIGURED")
        or parsed_base_url.scheme not in ("http", "https")
        or not parsed_base_url.netloc
        or parsed_base_url.path not in ("", "/")
        or parsed_base_url.query
        or parsed_base_url.fragment
        or parsed_base_url.username
        or parsed_base_url.password
        or (is_live_key and parsed_base_url.scheme != "https")
    ):
        return None
    return {
        "secret_key": secret_key,
        "webhook_secret": webhook_secret,
        "founders_coupon_id": str(app.config.get("STRIPE_FOUNDERS_COUPON_ID", "")).strip(),
        "public_base_url": public_base_url,
    }


class StripeAPIError(Exception):
    """A Stripe API failure with a safe message for the browser."""


def stripe_api_request(settings, method, path, payload=None):
    """Make a server-authenticated Stripe API request."""
    body = urllib.parse.urlencode(payload).encode("utf-8") if payload is not None else None
    authorization = "Basic " + base64.b64encode(
        (settings["secret_key"] + ":").encode("utf-8")
    ).decode("ascii")
    api_request = urllib.request.Request(
        "https://api.stripe.com" + path,
        data=body,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(api_request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise TypeError("Stripe response was not an object")
        return result
    except urllib.error.HTTPError as exc:
        app.logger.warning("Stripe returned HTTP %s for %s %s", exc.code, method, path)
        raise StripeAPIError("Card checkout could not be completed. Please try again.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        app.logger.warning("Stripe connection failed: %s", getattr(exc, "reason", str(exc)))
        raise StripeAPIError("Card checkout is temporarily unavailable. Please try again.") from exc
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        app.logger.exception("Stripe returned an invalid response")
        raise StripeAPIError("Stripe returned an unexpected response. Please try again.") from exc


def paypal_api_request(settings, method, path, payload=None):
    """Make one authenticated PayPal REST request without exposing credentials."""
    token_request = urllib.request.Request(
        settings["base_url"] + "/v1/oauth2/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii"),
        headers={
            "Authorization": "Basic "
            + base64.b64encode(
                f"{settings['client_id']}:{settings['client_secret']}".encode("utf-8")
            ).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(token_request, timeout=15) as response:
            access_token = json.loads(response.read().decode("utf-8"))["access_token"]
        authorization = "Bearer" + chr(32) + access_token
        api_request = urllib.request.Request(
            settings["base_url"] + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        with urllib.request.urlopen(api_request, timeout=15) as response:
            response_body = response.read().decode("utf-8")
        return json.loads(response_body) if response_body else {}
    except urllib.error.HTTPError as exc:
        app.logger.warning("PayPal returned HTTP %s for %s %s", exc.code, method, path)
        raise PayPalAPIError("PayPal could not complete that request. Please try again.") from exc
    except urllib.error.URLError as exc:
        app.logger.warning("PayPal connection failed: %s", exc.reason)
        raise PayPalAPIError("PayPal is temporarily unavailable. Please try again.") from exc
    except (KeyError, json.JSONDecodeError) as exc:
        app.logger.exception("PayPal returned an invalid response")
        raise PayPalAPIError("PayPal returned an unexpected response. Please try again.") from exc


def _paypal_failure(message, status=503):
    return jsonify({"error": message}), status


def _safe_next_url(value):
    if not value:
        return url_for("account")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or "\\" in value
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        return url_for("account")
    return value


def _upsert_subscription(provider, provider_id, user_id, property_type, status, period_end=None, customer_id=None):
    if not isinstance(user_id, str) or not isinstance(property_type, str) or property_type not in SUBSCRIPTION_PLANS:
        app.logger.warning("Ignoring provider subscription event with invalid account or plan metadata")
        return None
    user = db.session.get(User, user_id)
    if user is None:
        app.logger.warning("Ignoring provider subscription event for an unknown customer account")
        return None
    if not isinstance(period_end, int):
        period_end = None
    record = Subscription.query.filter_by(
        provider=provider,
        provider_subscription_id=provider_id,
    ).one_or_none()
    if record is None:
        record = Subscription(
            user_id=user.id,
            provider=provider,
            provider_subscription_id=provider_id,
            property_type=property_type,
            status=status,
            current_period_end=period_end,
        )
        db.session.add(record)
    elif record.user_id != user.id or record.property_type != property_type:
        app.logger.error("Provider subscription identity conflict; refusing to reassign ownership")
        return None
    else:
        record.status = status
        record.current_period_end = period_end
    if provider == "stripe" and customer_id and not user.stripe_customer_id:
        user.stripe_customer_id = customer_id
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = Subscription.query.filter_by(
            provider=provider,
            provider_subscription_id=provider_id,
        ).one_or_none()
        if existing and existing.user_id == user.id and existing.property_type == property_type:
            return existing
        app.logger.exception("Could not persist provider subscription state")
        return None
    return record


def _paypal_subscription_account(subscription):
    account_id = subscription.get("custom_id")
    user = db.session.get(User, account_id) if isinstance(account_id, str) else None
    plan_id = subscription.get("plan_id")
    property_type = next(
        (key for key, plan in SUBSCRIPTION_PLANS.items() if plan["paypal_plan_id"] == plan_id),
        None,
    )
    if user is None or property_type is None:
        return None
    return user, property_type


def _sync_paypal_subscription(subscription_id):
    settings = _paypal_settings()
    if settings is None:
        return None
    try:
        remote = paypal_api_request(
            settings,
            "GET",
            f"/v1/billing/subscriptions/{urllib.parse.quote(subscription_id, safe='')}",
        )
    except PayPalAPIError:
        app.logger.exception("Could not retrieve PayPal subscription for account synchronization")
        return None
    identity = _paypal_subscription_account(remote)
    if (
        identity is None
        or not _paypal_plan_matches_price(settings, identity[1], remote.get("plan_id"))
        or remote.get("id") != subscription_id
    ):
        app.logger.warning("Ignoring PayPal subscription event with mismatched account or plan")
        return None
    user, property_type = identity
    return _upsert_subscription(
        "paypal",
        subscription_id,
        user.id,
        property_type,
        remote.get("status", "UNKNOWN"),
    )


def _record_stripe_subscription(subscription):
    subscription_id = subscription.get("id")
    metadata = subscription.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    account_id = metadata.get("user_id")
    property_type = metadata.get("property_type")
    plan = _plan_configuration(property_type)
    items = subscription.get("items")
    item_data = items.get("data") if isinstance(items, dict) else None
    expected_price = (
        isinstance(item_data, list)
        and any(
            isinstance(item, dict)
            and isinstance(item.get("price"), dict)
            and item["price"].get("id") == plan["stripe_price_id"]
            for item in item_data
        )
    ) if plan else False
    if (
        not isinstance(subscription_id, str)
        or not subscription_id.startswith("sub_")
        or not expected_price
        or subscription.get("status") not in (
            "active", "trialing", "past_due", "unpaid", "canceled",
            "incomplete", "incomplete_expired", "paused",
        )
    ):
        app.logger.warning("Ignoring Stripe subscription event with invalid account or plan metadata")
        return None
    customer_id = subscription.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    return _upsert_subscription(
        "stripe",
        subscription_id,
        account_id,
        property_type,
        subscription["status"],
        subscription.get("current_period_end"),
        customer_id if isinstance(customer_id, str) else None,
    )


def _verify_stripe_signature(payload, signature_header, secret):
    fields = {}
    for field in signature_header.split(","):
        key, separator, value = field.partition("=")
        if separator:
            fields.setdefault(key, []).append(value)
    timestamps = fields.get("t", [])
    signatures = fields.get("v1", [])
    if not timestamps or not signatures:
        return False
    try:
        timestamp = int(timestamps[0])
    except ValueError:
        return False
    if abs(int(time.time()) - timestamp) > 300:
        return False
    signed_payload = str(timestamp).encode("ascii") + b"." + payload
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, signature) for signature in signatures)


def _verify_paypal_webhook(settings, event):
    webhook_id = str(app.config.get("PAYPAL_WEBHOOK_ID", "")).strip()
    if not webhook_id:
        return False
    payload = {
        "auth_algo": request.headers.get("PAYPAL-AUTH-ALGO", ""),
        "cert_url": request.headers.get("PAYPAL-CERT-URL", ""),
        "transmission_id": request.headers.get("PAYPAL-TRANSMISSION-ID", ""),
        "transmission_sig": request.headers.get("PAYPAL-TRANSMISSION-SIG", ""),
        "transmission_time": request.headers.get("PAYPAL-TRANSMISSION-TIME", ""),
        "webhook_id": webhook_id,
        "webhook_event": event,
    }
    try:
        result = paypal_api_request(
            settings,
            "POST",
            "/v1/notifications/verify-webhook-signature",
            payload,
        )
    except PayPalAPIError:
        app.logger.exception("Could not verify PayPal webhook signature")
        return False
    return result.get("verification_status") == "SUCCESS"


def _plan_configuration(property_type):
    if property_type not in SUBSCRIPTION_PLANS:
        return None
    return SUBSCRIPTION_PLANS[property_type]


def _public_subscription_plans():
    return [
        {
            "key": property_type,
            "label": plan["label"],
            "monthly_price": f"{plan['monthly_price']:.2f}",
            "stripe_available": bool(
                _stripe_settings()
                and str(plan["stripe_price_id"]).startswith("price_")
            ),
            "paypal_available": bool(
                _paypal_settings()
                and str(plan["paypal_plan_id"]).startswith("P-")
            ),
        }
        for property_type, plan in SUBSCRIPTION_PLANS.items()
    ]


def _stripe_founders_coupon(settings):
    coupon_id = settings.get("founders_coupon_id", "")
    if not coupon_id or len(coupon_id) > 255 or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in coupon_id
    ):
        return None
    try:
        coupon = stripe_api_request(
            settings,
            "GET",
            f"/v1/coupons/{urllib.parse.quote(coupon_id, safe='')}",
        )
    except StripeAPIError:
        app.logger.exception("Could not verify the configured Stripe founding offer")
        return None
    if (
        coupon.get("id") == coupon_id
        and coupon.get("valid") is True
        and coupon.get("percent_off") == 25
        and coupon.get("duration") == "repeating"
        and coupon.get("duration_in_months") == 3
        and coupon.get("max_redemptions") == 10
        and isinstance(coupon.get("times_redeemed"), int)
        and coupon["times_redeemed"] < 10
    ):
        return coupon
    return None


def _paypal_plan_matches_price(settings, property_type, plan_id):
    plan = _plan_configuration(property_type)
    if settings is None or plan is None or plan_id != plan["paypal_plan_id"]:
        return False
    try:
        paypal_plan = paypal_api_request(
            settings,
            "GET",
            f"/v1/billing/plans/{urllib.parse.quote(plan_id, safe='')}",
        )
    except PayPalAPIError:
        app.logger.exception("Could not verify the configured PayPal monthly plan")
        return False
    billing_cycles = paypal_plan.get("billing_cycles")
    if not isinstance(billing_cycles, list):
        return False
    for cycle in billing_cycles:
        if not isinstance(cycle, dict) or cycle.get("tenure_type") != "REGULAR":
            continue
        frequency = cycle.get("frequency")
        pricing = cycle.get("pricing_scheme")
        fixed_price = pricing.get("fixed_price") if isinstance(pricing, dict) else None
        if (
            paypal_plan.get("id") == plan_id
            and paypal_plan.get("status") == "ACTIVE"
            and isinstance(frequency, dict)
            and frequency.get("interval_unit") == "MONTH"
            and frequency.get("interval_count") == 1
            and isinstance(fixed_price, dict)
            and fixed_price.get("currency_code") == settings["currency"]
        ):
            try:
                if Decimal(str(fixed_price.get("value"))) == plan["monthly_price"]:
                    return True
            except (InvalidOperation, TypeError):
                return False
    return False


def _session_has_subscription(property_type):
    if not current_user.is_authenticated:
        return False
    now = int(time.time())
    return Subscription.query.filter(
        Subscription.user_id == current_user.id,
        Subscription.property_type == property_type,
        Subscription.status.in_(("active", "trialing")),
        db.or_(Subscription.current_period_end.is_(None), Subscription.current_period_end > now),
    ).first() is not None


def _stripe_studio_redirect():
    return redirect(url_for("pdf_studio") + "#plans", code=303)


def _subscription_error(property_type):
    plan = _plan_configuration(property_type)
    name = plan["label"] if plan else "this property type"
    return (
        f"An active monthly {name} subscription is required to download this PDF. "
        "Choose a plan below to subscribe."
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("account"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        if (
            len(email) > 254
            or email.count("@") != 1
            or "." not in email.rsplit("@", 1)[-1]
        ):
            error = "Enter a valid email address."
        elif len(password) < 12 or len(password) > 128:
            error = "Use a password between 12 and 128 characters."
        else:
            user = User(email=email, password_hash=generate_password_hash(password))
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                error = "An account with that email already exists. Sign in instead."
            else:
                login_user(user, remember=True, fresh=True)
                return redirect(_safe_next_url(request.args.get("next")))
    return render_template("auth.html", mode="register", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("account"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).one_or_none()
        if user is None or not check_password_hash(user.password_hash, password):
            error = "Email or password is incorrect."
        else:
            login_user(user, remember=True, fresh=True)
            return redirect(_safe_next_url(request.args.get("next")))
    return render_template("auth.html", mode="login", error=error)


@app.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.get("/account")
@login_required
def account():
    subscriptions = Subscription.query.filter_by(user_id=current_user.id).order_by(
        Subscription.updated_at.desc()
    ).all()
    return render_template(
        "account.html",
        subscriptions=subscriptions,
        property_types=PROPERTY_TYPES,
        message=session.pop("account_message", None),
    )


@app.post("/billing/stripe/portal")
@login_required
def stripe_billing_portal():
    settings = _stripe_settings()
    if settings is None or not current_user.stripe_customer_id:
        session["account_message"] = "Stripe billing management is not available for this account."
        return redirect(url_for("account"))
    try:
        portal = stripe_api_request(
            settings,
            "POST",
            "/v1/billing_portal/sessions",
            {
                "customer": current_user.stripe_customer_id,
                "return_url": settings["public_base_url"] + url_for("account"),
            },
        )
    except StripeAPIError:
        app.logger.exception("Could not create Stripe billing portal session")
        session["account_message"] = "Stripe billing management is temporarily unavailable."
        return redirect(url_for("account"))
    portal_url = portal.get("url")
    if not isinstance(portal_url, str) or not portal_url.startswith("https://billing.stripe.com/"):
        app.logger.warning("Stripe returned an invalid billing portal URL")
        session["account_message"] = "Stripe billing management is temporarily unavailable."
        return redirect(url_for("account"))
    return redirect(portal_url, code=303)


@app.post("/billing/paypal/<int:subscription_row_id>/cancel")
@login_required
def cancel_paypal_subscription(subscription_row_id):
    record = Subscription.query.filter_by(
        id=subscription_row_id,
        user_id=current_user.id,
        provider="paypal",
        status="ACTIVE",
    ).one_or_none()
    settings = _paypal_settings()
    if record is None or settings is None:
        session["account_message"] = "That active PayPal subscription could not be found."
        return redirect(url_for("account"))
    try:
        paypal_api_request(
            settings,
            "POST",
            f"/v1/billing/subscriptions/{urllib.parse.quote(record.provider_subscription_id, safe='')}/cancel",
            {"reason": "Customer requested cancellation from their Wholesale Scout account."},
        )
    except PayPalAPIError:
        app.logger.exception("Could not cancel PayPal subscription")
        session["account_message"] = "PayPal could not cancel the subscription. Please try again or use PayPal directly."
        return redirect(url_for("account"))
    record.status = "CANCELLED"
    db.session.commit()
    session["account_message"] = "PayPal confirmed the cancellation. Subscription PDF access has ended."
    return redirect(url_for("account"))


@app.post("/checkout/card/<property_type>")
@login_required
def start_card_checkout(property_type):
    settings = _stripe_settings()
    plan = _plan_configuration(property_type)
    if plan is None:
        return redirect(url_for("pdf_studio") + "#plans", code=303)
    if settings is None or not str(plan["stripe_price_id"]).startswith("price_"):
        session["stripe_payment_message"] = "Card subscriptions are not configured for this property plan yet."
        return _stripe_studio_redirect()

    success_url = (
        settings["public_base_url"]
        + url_for("stripe_subscription_success")
        + "?session_id={CHECKOUT_SESSION_ID}&property_type="
        + urllib.parse.quote(property_type, safe="")
    )
    cancel_url = (
        settings["public_base_url"]
        + url_for("pdf_studio")
        + "?card_checkout=cancelled&property_type="
        + urllib.parse.quote(property_type, safe="")
        + "#plans"
    )
    try:
        price = stripe_api_request(
            settings,
            "GET",
            f"/v1/prices/{urllib.parse.quote(plan['stripe_price_id'], safe='')}",
        )
    except StripeAPIError:
        app.logger.exception("Could not validate the configured Stripe subscription price")
        session["stripe_payment_message"] = "Card subscriptions are temporarily unavailable. Please try again later."
        return _stripe_studio_redirect()

    expected_amount = int(plan["monthly_price"] * 100)
    recurring = price.get("recurring")
    if (
        price.get("id") != plan["stripe_price_id"]
        or price.get("active") is not True
        or price.get("currency", "").upper() != "GBP"
        or price.get("unit_amount") != expected_amount
        or not isinstance(recurring, dict)
        or recurring.get("interval") != "month"
        or recurring.get("interval_count") != 1
    ):
        app.logger.warning("Stripe subscription price does not match the advertised monthly plan")
        session["stripe_payment_message"] = "This monthly plan is being configured. Please try again later."
        return _stripe_studio_redirect()

    payload = {
        "ui_mode": "hosted_page",
        "mode": "subscription",
        "billing_address_collection": "auto",
        "allow_promotion_codes": "false",
        "payment_method_collection": "always",
        "submit_type": "auto",
        "integration_identifier": "hosted_web_0002",
        "origin_context": "web",
        "line_items[0][price]": plan["stripe_price_id"],
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": current_user.id,
        "subscription_data[metadata][user_id]": current_user.id,
        "subscription_data[metadata][property_type]": property_type,
    }
    if current_user.stripe_customer_id:
        payload["customer"] = current_user.stripe_customer_id
    else:
        payload["customer_email"] = current_user.email
    coupon_id = settings["founders_coupon_id"]
    if coupon_id:
        if _stripe_founders_coupon(settings) is None:
            app.logger.warning("Stripe founding coupon does not match the advertised 10-customer offer")
            session["stripe_payment_message"] = "The founding offer is fully redeemed or its configuration needs attention."
            return _stripe_studio_redirect()
        payload["discounts[0][coupon]"] = coupon_id
    try:
        checkout = stripe_api_request(
            settings,
            "POST",
            "/v1/checkout/sessions",
            payload,
        )
    except StripeAPIError:
        app.logger.exception("Could not create a Stripe subscription Checkout session")
        session["stripe_payment_message"] = "Card subscription checkout could not be started. Please try again later."
        return _stripe_studio_redirect()

    checkout_url = checkout.get("url")
    if not isinstance(checkout_url, str) or not checkout_url.startswith("https://checkout.stripe.com/"):
        app.logger.warning("Stripe did not return a hosted Checkout URL")
        session["stripe_payment_message"] = "Card subscription checkout could not be started. Please try again later."
        return _stripe_studio_redirect()
    return redirect(checkout_url, code=303)


@app.get("/checkout/card/success")
def stripe_subscription_success():
    if not current_user.is_authenticated:
        return redirect(url_for("login", next=request.full_path))
    settings = _stripe_settings()
    session_id = request.args.get("session_id", "")
    property_type = request.args.get("property_type", "")
    plan = _plan_configuration(property_type)
    valid_session_id = (
        session_id.startswith("cs_")
        and len(session_id) <= 255
        and all(character.isalnum() or character in "_-" for character in session_id)
    )
    if settings is None or not valid_session_id or plan is None:
        session["stripe_payment_message"] = "We could not verify the subscription with Stripe."
        return _stripe_studio_redirect()

    encoded_session_id = urllib.parse.quote(session_id, safe="")
    path = f"/v1/checkout/sessions/{encoded_session_id}?" + urllib.parse.urlencode(
        {"expand[]": ["line_items", "subscription"]}
    )
    try:
        checkout = stripe_api_request(settings, "GET", path)
    except StripeAPIError:
        app.logger.exception("Could not verify a Stripe subscription Checkout session")
        session["stripe_payment_message"] = "We could not verify the subscription with Stripe. Please try again."
        return _stripe_studio_redirect()

    line_items = checkout.get("line_items")
    line_item_data = line_items.get("data") if isinstance(line_items, dict) else None
    expected_price_is_present = (
        isinstance(line_item_data, list)
        and any(
            isinstance(item, dict)
            and isinstance(item.get("price"), dict)
            and item["price"].get("id") == plan["stripe_price_id"]
            and item["price"].get("recurring", {}).get("interval") == "month"
            and item.get("quantity") == 1
            for item in line_item_data
        )
    )
    subscription = checkout.get("subscription")
    if isinstance(subscription, str):
        subscription_id = subscription
        try:
            subscription = stripe_api_request(
                settings,
                "GET",
                f"/v1/subscriptions/{urllib.parse.quote(subscription_id, safe='')}?"
                + urllib.parse.urlencode({"expand[]": "items.data.price"}),
            )
        except StripeAPIError:
            app.logger.exception("Could not verify the new Stripe subscription")
            subscription = {}
    else:
        subscription_id = subscription.get("id", "") if isinstance(subscription, dict) else ""
    subscription_items = subscription.get("items") if isinstance(subscription, dict) else None
    subscription_data = subscription_items.get("data") if isinstance(subscription_items, dict) else None
    active_subscription = (
        isinstance(subscription, dict)
        and subscription.get("id") == subscription_id
        and subscription.get("status") in ("active", "trialing")
        and isinstance(subscription_data, list)
        and any(
            isinstance(item, dict)
            and isinstance(item.get("price"), dict)
            and item["price"].get("id") == plan["stripe_price_id"]
            for item in subscription_data
        )
    )
    if (
        checkout.get("id") != session_id
        or checkout.get("mode") != "subscription"
        or checkout.get("status") != "complete"
        or checkout.get("client_reference_id") != current_user.id
        or not expected_price_is_present
        or not active_subscription
    ):
        session["stripe_payment_message"] = "Stripe has not confirmed an active monthly subscription for this plan."
        return _stripe_studio_redirect()

    subscription_record = _record_stripe_subscription(subscription)
    if subscription_record is None:
        session["stripe_payment_message"] = "Stripe confirmed checkout, but account access could not be saved. Please contact support."
        return _stripe_studio_redirect()
    return render_template(
        "stripe_success.html",
        property_label=plan["label"],
    )


@app.get("/")
def index():
    return render_template(
        "index.html",
        result=None,
        error=None,
        form_data={},
        property_types=PROPERTY_TYPES,
    )


@app.post("/")
def calculate():
    form_data = request.form.to_dict()
    result = None
    error = None
    try:
        if "photo" in request.files and request.files["photo"].filename:
            photo = request.files["photo"]
            suffix = Path(photo.filename).suffix[:12]
            with tempfile.NamedTemporaryFile(suffix=suffix) as temporary_file:
                photo.save(temporary_file.name)
                form_data["summary"] = analyze_property_photo(Path(temporary_file.name))
        result = _parse_property_form(form_data)
        if request.form.get("action") == "download_pdf":
            if not _session_has_subscription(result["property_type"]):
                return redirect(
                    url_for("pdf_studio", property_type=result["property_type"]) + "#plans",
                    code=303,
                )
            pdf = create_property_pdf(result)
            filename = f"{result['property_type']}_property_report.pdf"
            return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=filename)
    except (ValueError, RuntimeError, OSError) as exc:
        error = str(exc)
    return render_template(
        "index.html",
        result=result,
        error=error,
        form_data=form_data,
        property_types=PROPERTY_TYPES,
    )


@app.route("/pdf-studio", methods=["GET", "POST"])
def pdf_studio():
    result = None
    error = None
    form_data = request.form.to_dict() if request.method == "POST" else {}
    payment_message = session.pop("stripe_payment_message", None)
    if request.args.get("card_checkout") == "cancelled":
        payment_message = "Subscription checkout was cancelled. No subscription was activated."
    if request.method == "POST":
        try:
            result = _parse_property_form(form_data)
            if request.form.get("action") == "download_pdf":
                if not _session_has_subscription(result["property_type"]):
                    error = _subscription_error(result["property_type"])
                else:
                    pdf = create_property_pdf(result)
                    filename = f"{result['property_type']}_property_report.pdf"
                    return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=filename)
        except ValueError as exc:
            error = str(exc)
    stripe_settings = _stripe_settings()
    subscription_plans = _public_subscription_plans()
    for plan in subscription_plans:
        plan["sample_download_available"] = _session_has_subscription(plan["key"])
    return render_template(
        "pdf_studio.html",
        result=result,
        error=error,
        form_data=form_data,
        property_types=PROPERTY_TYPES,
        payment_message=payment_message,
        subscription_plans=subscription_plans,
        founders_offer=bool(
            stripe_settings
            and any(plan["stripe_available"] for plan in subscription_plans)
            and _stripe_founders_coupon(stripe_settings)
        ),
    )


@app.get("/pdf-studio/sample/<property_type>")
@login_required
def download_sample_property_pdf(property_type):
    sample = SAMPLE_PROPERTY_REPORTS.get(property_type)
    if sample is None:
        abort(404)
    if not _session_has_subscription(property_type):
        session["stripe_payment_message"] = _subscription_error(property_type)
        return redirect(url_for("pdf_studio", property_type=property_type) + "#plans")

    result = _parse_property_form({
        **sample,
        "property_type": property_type,
        "notes": "Sample report for product preview. Not a real property listing or investment advice.",
    })
    pdf = create_property_pdf(result)
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=sample["filename"],
    )


@app.post("/webhooks/stripe")
@csrf.exempt
def stripe_webhook():
    settings = _stripe_settings()
    webhook_secret = str(app.config.get("STRIPE_WEBHOOK_SECRET", "")).strip()
    raw_body = request.get_data(cache=True)
    signature = request.headers.get("Stripe-Signature", "")
    if settings is None or not webhook_secret:
        return jsonify({"error": "Stripe webhook is not configured."}), 503
    if not _verify_stripe_signature(raw_body, signature, webhook_secret):
        return jsonify({"error": "Invalid Stripe webhook signature."}), 400
    try:
        event = json.loads(raw_body)
    except (TypeError, json.JSONDecodeError):
        return jsonify({"error": "Invalid webhook payload."}), 400
    if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
        return jsonify({"error": "Invalid webhook payload."}), 400
    event_type = event.get("type")
    resource = event["data"].get("object")
    subscription_id = None
    if isinstance(resource, dict):
        if event_type == "checkout.session.completed":
            subscription_id = resource.get("subscription")
            if isinstance(subscription_id, dict):
                subscription_id = subscription_id.get("id")
        elif isinstance(event_type, str) and event_type.startswith("customer.subscription."):
            subscription_id = resource.get("id")
    if not subscription_id:
        return jsonify({"received": True})
    try:
        remote = stripe_api_request(
            settings,
            "GET",
            f"/v1/subscriptions/{urllib.parse.quote(str(subscription_id), safe='')}?"
            + urllib.parse.urlencode({"expand[]": "items.data.price"}),
        )
    except StripeAPIError:
        app.logger.exception("Stripe webhook subscription retrieval failed; asking Stripe to retry")
        return jsonify({"error": "Could not synchronize the subscription."}), 503
    _record_stripe_subscription(remote)
    return jsonify({"received": True})


@app.post("/webhooks/paypal")
@csrf.exempt
def paypal_webhook():
    settings = _paypal_settings()
    if settings is None or not app.config.get("PAYPAL_WEBHOOK_ID"):
        return jsonify({"error": "PayPal webhook is not configured."}), 503
    event = request.get_json(silent=True)
    if not isinstance(event, dict):
        return jsonify({"error": "Invalid webhook payload."}), 400
    if not _verify_paypal_webhook(settings, event):
        return jsonify({"error": "Invalid PayPal webhook signature."}), 400
    event_type = event.get("event_type")
    resource = event.get("resource")
    if (
        isinstance(event_type, str)
        and event_type.startswith("BILLING.SUBSCRIPTION.")
        and isinstance(resource, dict)
        and isinstance(resource.get("id"), str)
    ):
        if _sync_paypal_subscription(resource["id"]) is None:
            app.logger.warning("PayPal webhook could not be matched to an account subscription")
    return jsonify({"received": True})


@app.get("/api/paypal/config")
def paypal_config():
    settings = _paypal_settings()
    paypal_plans = {
        key: plan["paypal_plan_id"]
        for key, plan in SUBSCRIPTION_PLANS.items()
        if (
            settings
            and current_user.is_authenticated
            and str(plan["paypal_plan_id"]).startswith("P-")
            and _paypal_plan_matches_price(settings, key, plan["paypal_plan_id"])
        )
    }
    return jsonify({
        "available": bool(settings and paypal_plans),
        "client_id": settings["client_id"] if settings and paypal_plans else "",
        "plan_ids": paypal_plans,
        "currency": settings["currency"] if settings else "GBP",
        "account_id": current_user.id if current_user.is_authenticated else "",
    })


@app.post("/api/paypal/subscriptions/verify")
@login_required
def verify_paypal_subscription():
    settings = _paypal_settings()
    if settings is None:
        return _paypal_failure("PayPal subscriptions are not configured on this server.")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _paypal_failure("A JSON subscription request is required.", 400)
    property_type = payload.get("property_type", "")
    subscription_id = payload.get("subscription_id", "")
    if (
        property_type not in SUBSCRIPTION_PLANS
        or not isinstance(subscription_id, str)
        or not subscription_id.startswith("I-")
        or len(subscription_id) > 64
        or not all(character.isalnum() or character in "-_" for character in subscription_id)
    ):
        return _paypal_failure("A valid property plan and PayPal subscription are required.", 400)
    try:
        subscription = paypal_api_request(
            settings,
            "GET",
            f"/v1/billing/subscriptions/{urllib.parse.quote(subscription_id, safe='')}",
        )
    except PayPalAPIError:
        app.logger.exception("Could not verify PayPal subscription for customer account")
        return _paypal_failure("PayPal subscription status is temporarily unavailable.")
    identity = _paypal_subscription_account(subscription)
    if (
        identity is None
        or identity[0].id != current_user.id
        or identity[1] != property_type
        or subscription.get("status") != "ACTIVE"
        or not _paypal_plan_matches_price(settings, property_type, subscription.get("plan_id"))
    ):
        return _paypal_failure("PayPal has not confirmed an active monthly subscription for this plan.", 402)
    _upsert_subscription("paypal", subscription_id, current_user.id, property_type, "ACTIVE")
    return jsonify({"status": "ACTIVE", "message": "PayPal confirmed your monthly subscription."})


@app.get("/health")
def health():
    try:
        db.session.execute(db.select(1))
    except SQLAlchemyError:
        app.logger.exception("Database health check failed")
        return jsonify({"status": "unavailable"}), 503
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=False)
