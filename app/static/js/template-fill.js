(async function () {
    const T = window.TEMPLATE;
    const selSenderClient = document.getElementById("sender-client");
    const selSenderAccount = document.getElementById("sender-account");
    const selSenderCard = document.getElementById("sender-card");
    const selReceiverClient = document.getElementById("receiver-client");
    const selReceiverAccount = document.getElementById("receiver-account");
    const selReceiverCard = document.getElementById("receiver-card");
    const btnRender = document.getElementById("btn-render");
    const btnDownload = document.getElementById("btn-download");
    const result = document.getElementById("result-code");
    const unresolved = document.getElementById("unresolved-box");

    let clients = [];
    let lastResult = null;

    function clientLabel(c) {
        return (c.attributes?.fullName || c.id) + " (" + c.id.slice(0, 8) + "…)";
    }

    function fillClients(sel) {
        sel.innerHTML = `<option value="">— выбрать —</option>` + clients.map(c => `<option value="${c.id}">${TM.escapeHtml(clientLabel(c))}</option>`).join("");
    }

    async function loadAccounts(clientId, accSel, cardSel) {
        accSel.innerHTML = `<option value="">— первый —</option>`;
        cardSel.innerHTML = `<option value="">— первая —</option>`;
        if (!clientId) return;
        try {
            const accs = await TM.api("GET", `/api/accounts?client_id=${clientId}`);
            accSel.innerHTML += accs.map(a => `<option value="${a.id}">${TM.escapeHtml(a.attributes?.number || a.id)}</option>`).join("");
        } catch (e) { TM.toast("Не удалось загрузить счета: " + e.message, "error"); }
    }

    async function loadCards(accountId, cardSel) {
        cardSel.innerHTML = `<option value="">— первая —</option>`;
        if (!accountId) return;
        try {
            const cards = await TM.api("GET", `/api/cards?account_id=${accountId}`);
            cardSel.innerHTML += cards.map(c => `<option value="${c.id}">${TM.escapeHtml(c.attributes?.number || c.id)}</option>`).join("");
        } catch (e) { TM.toast("Не удалось загрузить карты: " + e.message, "error"); }
    }

    selSenderClient.addEventListener("change", () => loadAccounts(selSenderClient.value, selSenderAccount, selSenderCard));
    selSenderAccount.addEventListener("change", () => loadCards(selSenderAccount.value, selSenderCard));
    selReceiverClient.addEventListener("change", () => loadAccounts(selReceiverClient.value, selReceiverAccount, selReceiverCard));
    selReceiverAccount.addEventListener("change", () => loadCards(selReceiverAccount.value, selReceiverCard));

    btnRender.addEventListener("click", async () => {
        unresolved.hidden = true; unresolved.innerHTML = "";
        const body = {
            sender_client_id: selSenderClient.value || null,
            sender_account_id: selSenderAccount.value || null,
            sender_card_id: selSenderCard.value || null,
            receiver_client_id: selReceiverClient.value || null,
            receiver_account_id: selReceiverAccount.value || null,
            receiver_card_id: selReceiverCard.value || null,
        };
        try {
            lastResult = await TM.api("POST", `/api/templates/${T.id}/fill`, body);
            result.textContent = lastResult.content;
            btnDownload.disabled = false;
            if (lastResult.unresolved && lastResult.unresolved.length) {
                unresolved.hidden = false;
                unresolved.innerHTML = `<strong>Не подставлено:</strong> ${lastResult.unresolved.map(TM.escapeHtml).join(", ")}`;
            }
        } catch (e) { TM.toast(e.message, "error"); }
    });

    btnDownload.addEventListener("click", () => {
        if (!lastResult) return;
        const ext = lastResult.format === "xml" ? "xml" : "json";
        const blob = new Blob([lastResult.content], { type: ext === "xml" ? "application/xml" : "application/json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `filled-${Date.now()}.${ext}`;
        document.body.appendChild(a); a.click(); a.remove();
    });

    try {
        clients = await TM.api("GET", "/api/clients");
        fillClients(selSenderClient);
        fillClients(selReceiverClient);
    } catch (e) { TM.toast("Не удалось загрузить клиентов: " + e.message, "error"); }
})();
