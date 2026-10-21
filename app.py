import argparse
import os
from pathlib import Path

try:
    import google.generativeai as genai  # type: ignore
    AI_CLIENT_AVAILABLE = True
except ModuleNotFoundError:
    genai = None  # type: ignore
    AI_CLIENT_AVAILABLE = False

# Setup your AI Brain
# IMPORTANT: Set your API key via the AI_STUDIO_API_KEY environment variable
API_KEY = os.getenv("AI_STUDIO_API_KEY", "")
if AI_CLIENT_AVAILABLE:
    genai.configure(api_key=API_KEY)

# Example runs:
#   python app.py --purchase-price 200000 --monthly-rent 1200 --sq-ft 800 --summary "dated with worn fixtures"
#   python app.py --purchase-price 200000 --monthly-rent 1200 --sq-ft 800 --photo property.jpg

BASE_REFURB_RATE = 50.0  # £/sqft for standard refresh
FEE_RATE = 0.05
HEAVY_CONDITION_KEYWORDS = ["derelict", "dilapidated", "gutted", "ruin", "uninhabitable"]
AGED_CONDITION_KEYWORDS = ["dated", "worn", "old", "tired", "outdated", "shabby"]


def infer_refurbishment_multiplier(photo_analysis_summary: str) -> float:
    """Infer refurbishment multiplier from a property condition summary."""
    summary_lower = str(photo_analysis_summary or "").lower()

    if any(keyword in summary_lower for keyword in HEAVY_CONDITION_KEYWORDS):
        return 2.5
    if any(keyword in summary_lower for keyword in AGED_CONDITION_KEYWORDS):
        return 1.2
    return 1.0


def calculate_property_yield(purchase_price: float, monthly_rent: float, sq_ft: float, photo_analysis_summary: str) -> dict:
    """Calculate property investment yield and refurbishment estimates."""
    if purchase_price <= 0 or monthly_rent < 0 or sq_ft <= 0:
        raise ValueError("purchase_price, monthly_rent, and sq_ft must be positive numbers")

    refurb_multiplier = infer_refurbishment_multiplier(photo_analysis_summary)
    estimated_refurb = sq_ft * BASE_REFURB_RATE * refurb_multiplier
    total_investment = purchase_price + estimated_refurb + (purchase_price * FEE_RATE)

    annual_rent = monthly_rent * 12
    gross_yield = (annual_rent / purchase_price) * 100
    net_yield = (annual_rent / total_investment) * 100

    return {
        "estimated_refurb": round(estimated_refurb, 2),
        "total_capital_required": round(total_investment, 2),
        "gross_yield_percent": round(gross_yield, 2),
        "net_yield_percent": round(net_yield, 2),
        "deal_rating": "Gold" if net_yield > 8 else "Silver" if net_yield > 5 else "Bronze",
    }


def _extract_text_from_response(response) -> str:
    if response is None:
        return ""
    if hasattr(response, "output_text"):
        return response.output_text
    if isinstance(response, dict):
        for key in ("output_text", "text", "content"):
            if key in response:
                return str(response[key])
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, list) and content:
            item = content[0]
            if hasattr(item, "text"):
                return item.text
            if isinstance(item, dict) and "text" in item:
                return str(item["text"])
    return str(response)


def analyze_property_photo(photo_path: Path) -> str:
    """Analyze a property photo with Gemini Vision and return a text summary."""
    if not photo_path.exists():
        raise FileNotFoundError(f"Photo not found: {photo_path}")

    if not AI_CLIENT_AVAILABLE:
        raise RuntimeError("google.generativeai is not installed. Install it or provide --summary manually.")

    with photo_path.open("rb") as image_file:
        if hasattr(genai, "responses") and hasattr(genai.responses, "generate"):
            response = genai.responses.generate(
                model="gemini-1.0",
                input=[
                    {"role": "user", "content": "Please describe the condition of this property and any likely refurbishment needs."},
                    {"role": "user", "content": {"image": image_file}},
                ],
            )
        elif hasattr(genai, "Images") and hasattr(genai.Images, "generate"):
            response = genai.Images.generate(
                model="gemini-vision-preview",
                image=image_file,
                prompt="Describe the condition of this property and likely refurbishment requirements.",
            )
        else:
            raise RuntimeError(
                "The installed google.generativeai package does not expose a supported image analysis API. "
                "Please update the package or use --summary instead."
            )

    return _extract_text_from_response(response)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Property yield calculator with AI-assisted condition analysis.")
    parser.add_argument("--purchase-price", type=float, required=True, help="Purchase price of the property")
    parser.add_argument("--monthly-rent", type=float, required=True, help="Expected monthly rent")
    parser.add_argument("--sq-ft", type=float, required=True, help="Floor area in square feet")
    parser.add_argument("--summary", type=str, default="", help="AI or manual condition summary")
    parser.add_argument("--photo", type=Path, help="Optional property photo to analyze")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    photo_summary = args.summary

    if args.photo:
        try:
            photo_summary = analyze_property_photo(args.photo)
        except Exception as exc:
            print(f"Warning: could not analyze photo: {exc}")
            if not photo_summary:
                photo_summary = ""

    result = calculate_property_yield(
        purchase_price=args.purchase_price,
        monthly_rent=args.monthly_rent,
        sq_ft=args.sq_ft,
        photo_analysis_summary=photo_summary,
    )

    print("Property investment summary:")
    print(f"  Estimated refurb cost: £{result['estimated_refurb']}")
    print(f"  Total capital required: £{result['total_capital_required']}")
    print(f"  Gross yield: {result['gross_yield_percent']}%")
    print(f"  Net yield: {result['net_yield_percent']}%")
    print(f"  Deal rating: {result['deal_rating']}")


if __name__ == "__main__":
    import io
    import streamlit as st
    from PIL import Image

    st.title("Wholesale Scout Pro Report Generator")
    st.write("Here is an example layout of what our underwriting software produces:")

    image_path = "report_screenshot.png"
    img = Image.open(image_path)

    st.image(img, caption="Wholesale Scout Pro Standard 1-Page Layout", use_container_width=True)

    pdf_buffer = io.BytesIO()
    rgb_img = img.convert("RGB")
    rgb_img.save(pdf_buffer, format="PDF")
    pdf_bytes = pdf_buffer.getvalue()

    st.download_button(
        label="📥 Download Free Sample Report (PDF Format)",
        data=pdf_bytes,
        file_name="Wholesale_Scout_Pro_Sample.pdf",
        mime="application/pdf"
    )
