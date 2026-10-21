(function () {
    "use strict";

    const containers = document.querySelectorAll(".paypal-subscription-button");
    if (!containers.length) return;

    function setMessage(container, message, isError) {
        container.replaceChildren();
        const status = document.createElement("p");
        status.className = `checkout-message${isError ? " alert-error" : ""}`;
        status.setAttribute("role", "status");
        status.textContent = message;
        container.appendChild(status);
    }

    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

    async function start() {
        try {
            const response = await fetch("/api/paypal/config", {
                headers: { Accept: "application/json" },
            });
            const config = await response.json();
            if (!response.ok) throw new Error("Could not check PayPal subscription configuration.");
            if (!config.available) {
                containers.forEach((container) => setMessage(container, "PayPal subscriptions are not configured yet.", false));
                return;
            }

            const sdk = document.createElement("script");
            const parameters = new URLSearchParams({
                "client-id": config.client_id,
                currency: config.currency,
                intent: "subscription",
                vault: "true",
                components: "buttons",
            });
            sdk.src = `https://www.paypal.com/sdk/js?${parameters.toString()}`;
            sdk.onload = () => {
                if (!window.paypal) {
                    containers.forEach((container) => setMessage(container, "PayPal checkout could not load. Please try again later.", true));
                    return;
                }
                containers.forEach((container) => {
                    const propertyType = container.dataset.propertyType;
                    const planId = config.plan_ids[propertyType];
                    if (!planId) {
                        setMessage(container, "PayPal checkout is not set up for this plan yet.", false);
                        return;
                    }
                    container.replaceChildren();
                    window.paypal.Buttons({
                        style: {
                            layout: "horizontal",
                            color: "gold",
                            shape: "rect",
                            label: "subscribe",
                            height: 38,
                        },
                        createSubscription: (data, actions) => actions.subscription.create({
                            plan_id: planId,
                            custom_id: config.account_id,
                            application_context: {
                                brand_name: "Wholesale Scout",
                                user_action: "SUBSCRIBE_NOW",
                            },
                        }),
                        onApprove: async (data) => {
                            setMessage(container, "Checking your monthly subscription with PayPal…", false);
                            try {
                                const verified = await fetch("/api/paypal/subscriptions/verify", {
                                    method: "POST",
                                    headers: {
                                        "Content-Type": "application/json",
                                        Accept: "application/json",
                                        "X-CSRFToken": csrfToken,
                                    },
                                    body: JSON.stringify({
                                        property_type: propertyType,
                                        subscription_id: data.subscriptionID,
                                    }),
                                });
                                const result = await verified.json();
                                if (!verified.ok || result.status !== "ACTIVE") {
                                    throw new Error(result.error || "PayPal has not confirmed an active subscription.");
                                }
                                setMessage(container, "Subscription active. This property type's PDFs are now unlocked.", false);
                            } catch (error) {
                                setMessage(container, error.message || "PayPal could not confirm this subscription.", true);
                            }
                        },
                        onCancel: () => setMessage(container, "Checkout cancelled. No subscription was activated.", false),
                        onError: (error) => {
                            console.error("PayPal subscription checkout error", error);
                            setMessage(container, "PayPal checkout could not be completed. Please try again.", true);
                        },
                    }).render(container).catch((error) => {
                        console.error("PayPal subscription button could not render", error);
                        setMessage(container, "PayPal checkout could not load. Please try again later.", true);
                    });
                });
            };
            sdk.onerror = () => containers.forEach((container) => {
                setMessage(container, "PayPal could not be reached. Please check your connection.", true);
            });
            document.head.appendChild(sdk);
        } catch (error) {
            console.error("PayPal subscription configuration request failed", error);
            containers.forEach((container) => {
                setMessage(container, error.message || "PayPal subscriptions are temporarily unavailable.", true);
            });
        }
    }

    start();
})();
