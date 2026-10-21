(function () {
    "use strict";

    const panel = document.getElementById("assistant-panel");
    const launcher = document.querySelector(".assistant-launcher");
    const closeButton = document.querySelector(".assistant-close");
    const form = document.getElementById("assistant-form");
    const input = document.getElementById("assistant-input");
    const messages = document.getElementById("assistant-messages");
    if (!panel || !launcher || !form || !input || !messages) return;

    const paymentAnswers = [
        {
            matches: ["pricing", "monthly price", "monthly prices", "plan price", "subscription", "subscribe", "how much", "what does it cost"],
            answer: "PDF Studio lists these monthly prices: £8.99 for single-family rentals, £8.99 for apartment rentals, £12.99 for HMOs, £12.99 for renovation/flips, and £16.99 for commercial property. Create or sign in to an account before subscribing; an active subscription unlocks downloads only for its matching property type and stays linked to your account across devices. The calculator and PDF previews stay free. Check the plan cards for the payment methods currently configured.",
        },
        {
            matches: ["download", "unlock", "access", "paid pdf", "buy"],
            answer: "PDF downloads require an active subscription for the same property type. After checkout, the site checks subscription status with Stripe or PayPal before granting access. The browser session remembers the subscription; Scout cannot look up your account or restore access. If checkout completed but downloads remain locked, contact the site owner and do not send card details or payment credentials in chat.",
        },
        {
            matches: ["payment status", "check payment", "check my payment", "paid", "payment went through", "charge", "receipt", "billing"],
            answer: "Scout cannot see customer transactions or account billing details. After checkout, the site verifies the subscription with the payment provider and displays the result. Check your provider account or receipt for billing details. Never share card numbers, passwords, or API credentials in this chat.",
        },
        {
            matches: ["cancel", "cancellation", "refund", "stop paying"],
            answer: "Scout cannot cancel subscriptions or issue refunds for you. Sign in and open My account to manage Stripe billing through Stripe's portal or cancel a PayPal subscription. Refund requests and account-access problems should go to the site owner; never send payment credentials in chat.",
        },
        {
            matches: ["paypal", "stripe", "card", "payment method", "checkout"],
            answer: "Checkout uses hosted Stripe card checkout and PayPal subscriptions when those options are configured. The plan cards in PDF Studio show which methods are available right now. Payment details are entered with the provider, not in this chat or stored by Wholesale Scout. If a method says setup pending, that option is not ready for purchases.",
        },
    ];

    const answers = [
        {
            matches: ["realtor", "agent", "client", "buyer"],
            answer: "For Realtors and property professionals, Wholesale Scout puts key deal inputs in one place: compare rent, purchase price, refurbishment assumptions and estimated yield, then prepare a property-specific editable PDF to discuss with a client or team. It helps organize an initial review; it doesn't replace due diligence or guarantee a deal's performance.",
        },
        {
            matches: ["pdf", "report", "document", "editable"],
            answer: "Choose a single-family, apartment, HMO, renovation/flip or commercial report in PDF Studio. Enter the figures, preview the layout and download a fillable PDF. You can edit its form fields in a PDF reader; to recalculate the headline figures, update the inputs in PDF Studio and download it again.",
        },
        {
            matches: ["calculator", "calculate", "yield", "rent", "numbers", "example"],
            answer: "The homepage calculator updates as you enter price, rent, floor area and refurbishment assumptions. Try the rental, HMO or flip examples to see how changing the inputs affects the estimate. Results depend on your assumptions and aren't financial advice.",
        },
        {
            matches: ["hmo", "multi-let", "room"],
            answer: "Select HMO / multi-let and enter the monthly rent per room and number of rentable rooms. The estimate adds those rents together before calculating annual income and yield. Check licensing, occupancy and operating costs separately.",
        },
        {
            matches: ["flip", "renovation", "arv", "after repair"],
            answer: "For a renovation or flip, choose that strategy and enter your purchase price, refurbishment budget and estimated after-repair value. The tool shows an illustrative profit and return estimate; selling costs, financing, taxes, time and surprises can change the actual result.",
        },
        {
            matches: ["why", "benefit", "use", "worth", "help"],
            answer: "Wholesale Scout is designed to make an early deal review easier to explain: explore assumptions with a live calculator, compare common property strategies, and create an editable report from the same figures. It's a planning aid—not a promise of profit or a substitute for independent checks.",
        },
    ];

    function appendMessage(text, kind) {
        const message = document.createElement("p");
        message.className = `assistant-message ${kind === "user" ? "assistant-user" : "assistant-reply"}`;
        message.textContent = text;
        messages.appendChild(message);
        messages.scrollTop = messages.scrollHeight;
    }

    function respondTo(question) {
        const normalized = question.toLowerCase();
        const paymentMatches = paymentAnswers.filter((item) => item.matches.some((word) => normalized.includes(word)));
        const match = paymentMatches.length
            ? { answer: paymentMatches.map((item) => item.answer).join("\n\n") }
            : answers.find((item) => item.matches.some((word) => normalized.includes(word)));
        appendMessage(
            match
                ? match.answer
                : "I'm Scout, an automated product guide. Ask me about the calculator, rental or flip estimates, editable PDFs, or checkout. You can also try the calculator or open PDF Studio below.",
            "assistant"
        );
    }

    function submitQuestion(question) {
        const value = question.trim();
        if (!value) return;
        appendMessage(value, "user");
        respondTo(value);
        input.value = "";
    }

    function setOpen(isOpen) {
        panel.hidden = !isOpen;
        launcher.setAttribute("aria-expanded", String(isOpen));
        if (isOpen) input.focus();
        else launcher.focus();
    }

    launcher.addEventListener("click", () => setOpen(panel.hidden));
    closeButton?.addEventListener("click", () => setOpen(false));
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        submitQuestion(input.value);
    });
    document.querySelectorAll(".assistant-prompts button").forEach((button) => {
        button.addEventListener("click", () => submitQuestion(button.textContent));
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !panel.hidden) setOpen(false);
    });
})();
