(function () {
    "use strict";

    const examples = {
        rental: {
            property_type: "single_family_rental",
            property_address: "14 Willow Road, Leeds",
            purchase_price: 185000,
            monthly_rent: 1250,
            bedrooms: 1,
            sq_ft: 980,
            refurb_cost: "",
            arv: 0,
            summary: "Well maintained; minor cosmetic updates.",
        },
        hmo: {
            property_type: "hmo",
            property_address: "22 Park Street, Manchester",
            purchase_price: 285000,
            monthly_rent: 650,
            bedrooms: 5,
            sq_ft: 1600,
            refurb_cost: 28000,
            arv: 0,
            summary: "Five-room HMO conversion with a refreshed communal kitchen.",
        },
        flip: {
            property_type: "flip",
            property_address: "8 Station View, Bristol",
            purchase_price: 220000,
            monthly_rent: 0,
            bedrooms: 1,
            sq_ft: 1250,
            refurb_cost: 42000,
            arv: 330000,
            summary: "Full renovation; resale value based on comparable homes.",
        },
    };

    const propertyLabels = {
        single_family_rental: "Single-family rental",
        apartment_rental: "Apartment rental",
        hmo: "HMO / multi-let",
        flip: "Renovation / flip",
        commercial: "Commercial property",
    };

    function numberFrom(form, name, fallback) {
        const field = form.elements.namedItem(name);
        if (!field || field.value.trim() === "") return fallback;
        const value = Number(field.value);
        return Number.isFinite(value) ? value : fallback;
    }

    function money(value) {
        if (!Number.isFinite(value)) return "—";
        return new Intl.NumberFormat("en-GB", {
            style: "currency",
            currency: "GBP",
            maximumFractionDigits: 0,
        }).format(value);
    }

    function estimateRefurb(form, area) {
        const entered = form.elements.namedItem("refurb_cost").value.trim();
        if (entered !== "") return Math.max(0, Number(entered) || 0);
        const condition = (form.elements.namedItem("summary").value || "").toLowerCase();
        const multiplier = ["derelict", "dilapidated", "gutted", "ruin", "uninhabitable"].some((word) => condition.includes(word))
            ? 2.5
            : ["dated", "worn", "old", "tired", "outdated", "shabby"].some((word) => condition.includes(word))
                ? 1.2
                : 1;
        return Math.max(0, area * 50 * multiplier);
    }

    function updatePreview(form, result) {
        const put = (selector, value) => {
            const target = document.querySelector(selector);
            if (target) target.textContent = value;
        };
        const propertyType = form.elements.namedItem("property_type").value;
        const title = `${propertyLabels[propertyType] || "Property"} deal report`;
        const address = (form.elements.namedItem("property_address").value || "").trim();
        const notes = (form.elements.namedItem("notes")?.value || form.elements.namedItem("summary").value || "").trim();
        const validDeal = result.purchase > 0 && result.area > 0;

        put("[data-preview-title]", title);
        put("[data-preview-address]", address || "Property address");
        put("[data-preview-purchase]", validDeal ? money(result.purchase) : "£—");
        put("[data-preview-rent]", result.monthlyRent > 0 ? money(result.monthlyRent) : "£—");
        put("[data-preview-area]", result.area ? `${result.area.toLocaleString("en-GB")} sq ft` : "— sq ft");
        put("[data-preview-refurb]", validDeal ? money(result.refurb) : "£—");
        put("[data-preview-rating]", validDeal ? result.rating : "—");
        put("[data-preview-notes]", notes || "Your condition notes and assumptions appear here.");
        put("[data-preview-metric-label]", propertyType === "flip" ? "ESTIMATED FLIP PROFIT" : "NET RENTAL YIELD");
        put("[data-preview-metric]", result.headline);
    }

    function attachCalculator(form) {
        const typeField = form.elements.namedItem("property_type");
        const showConditionalFields = () => {
            const type = typeField.value;
            const hmoField = form.querySelector("[data-hmo-field]");
            const arvField = form.querySelector("[data-arv-field]");
            if (hmoField) hmoField.hidden = type !== "hmo";
            if (arvField) arvField.hidden = type !== "flip";
        };

        const calculate = () => {
            const propertyType = typeField.value;
            const purchase = numberFrom(form, "purchase_price", 0);
            const monthlyRent = numberFrom(form, "monthly_rent", 0);
            const area = numberFrom(form, "sq_ft", 0);
            const bedrooms = numberFrom(form, "bedrooms", 1);
            const arv = numberFrom(form, "arv", 0);
            const rooms = propertyType === "hmo" ? Math.max(1, bedrooms) : 1;
            const annualRent = monthlyRent * rooms * 12;
            const refurb = estimateRefurb(form, area);
            const capital = purchase + refurb + purchase * 0.05;
            const netYield = capital > 0 ? (annualRent / capital) * 100 : 0;
            const grossYield = purchase > 0 ? (annualRent / purchase) * 100 : 0;
            const flipProfit = arv - capital;
            const roi = capital > 0 ? (flipProfit / capital) * 100 : 0;
            const isFlip = propertyType === "flip";
            const rating = isFlip
                ? (roi > 20 ? "Gold" : roi > 10 ? "Silver" : "Bronze")
                : (netYield > 8 ? "Gold" : netYield > 5 ? "Silver" : "Bronze");
            const result = {
                purchase,
                monthlyRent,
                area,
                refurb,
                rating,
                headline: purchase > 0 && area > 0
                    ? (isFlip ? money(flipProfit) : `${netYield.toFixed(2)}%`)
                    : "—",
            };

            const put = (selector, value) => {
                const target = document.querySelector(selector);
                if (target) target.textContent = value;
            };
            put("[data-result-property]", propertyLabels[propertyType] || "Property deal");
            put("[data-result-headline-label]", isFlip ? "Estimated flip profit" : "Net rental yield");
            put("[data-result-headline]", result.headline);
            put("[data-result-rating]", purchase > 0 && area > 0 ? rating : "—");
            put("[data-result-gross]", purchase > 0 ? `${grossYield.toFixed(2)}%` : "—");
            put("[data-result-refurb]", area > 0 ? money(refurb) : "—");
            put("[data-result-capital]", purchase > 0 && area > 0 ? money(capital) : "—");
            put("[data-result-income]", annualRent > 0 ? money(annualRent) : "—");
            updatePreview(form, result);
            showConditionalFields();
        };

        form.addEventListener("input", calculate);
        form.addEventListener("change", calculate);
        typeField.addEventListener("change", calculate);
        calculate();
    }

    document.querySelectorAll("[data-live-calculator]").forEach(attachCalculator);
    document.querySelectorAll("[data-example]").forEach((button) => {
        button.addEventListener("click", () => {
            const form = document.querySelector("[data-live-calculator]");
            const example = examples[button.dataset.example];
            if (!form || !example) return;
            Object.entries(example).forEach(([name, value]) => {
                const field = form.elements.namedItem(name);
                if (field) field.value = value;
            });
            form.dispatchEvent(new Event("input", { bubbles: true }));
            form.dispatchEvent(new Event("change", { bubbles: true }));
        });
    });
})();
