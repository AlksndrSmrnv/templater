(async function () {
    const T = window.TEMPLATE;
    const rolePanels = document.getElementById("role-panels");
    const btnRender = document.getElementById("btn-render");
    const btnDownload = document.getElementById("btn-download");
    const result = document.getElementById("result-code");
    const unresolved = document.getElementById("unresolved-box");

    const roles = [
        { key: "sender", title: "Отправитель", requestPrefix: "sender" },
        { key: "receiver", title: "Получатель", requestPrefix: "receiver" },
    ];
    if (T.hasAccountOwner) {
        roles.push({
            key: "accountOwner",
            title: "Владелец счёта",
            requestPrefix: "account_owner",
        });
    }

    const roleByKey = new Map(roles.map(role => [role.key, role]));
    const state = new Map(roles.map(role => [
        role.key,
        {
            clientId: null,
            accountId: null,
            cardId: null,
            clientQuery: "",
            accounts: [],
            cards: [],
            loading: false,
            loadToken: null,
        },
    ]));

    let clients = [];
    let clientsLoading = true;
    let lastResult = null;

    function roleState(roleKey) {
        return state.get(roleKey);
    }

    function clientLabel(client) {
        const attrs = client.attributes || {};
        return attrs.fullName || attrs.name || attrs.shortName || attrs.inn || client.description || client.id;
    }

    function entityNumber(entity) {
        return entity.attributes?.number || entity.id;
    }

    function entityDescription(entity) {
        return entity.description || "Без описания";
    }

    function accountNumberById(roleKey) {
        return new Map(roleState(roleKey).accounts.map(account => [account.id, entityNumber(account)]));
    }

    function filteredClients(roleKey) {
        const query = roleState(roleKey).clientQuery.trim().toLowerCase();
        if (!query) return clients;
        return clients.filter(client => {
            const attrs = client.attributes || {};
            const text = [
                clientLabel(client),
                client.description,
                client.id,
                attrs.fullName,
                attrs.name,
                attrs.shortName,
                attrs.inn,
            ].filter(Boolean).join(" ").toLowerCase();
            return text.includes(query);
        });
    }

    function selectedClass(isSelected) {
        return isSelected ? " selected" : "";
    }

    function renderClientList(role) {
        const rs = roleState(role.key);
        if (clientsLoading) {
            return `<div class="picker-empty">Загрузка клиентов...</div>`;
        }
        const items = filteredClients(role.key);
        if (!items.length) {
            return `<div class="picker-empty">Клиенты не найдены</div>`;
        }
        return items.map(client => `
            <button type="button"
                    class="picker-row${selectedClass(rs.clientId === client.id)}"
                    data-action="select-client"
                    data-role="${role.key}"
                    data-id="${client.id}">
                <span class="picker-row-main">${TM.escapeHtml(clientLabel(client))}</span>
                <span class="picker-row-sub">${TM.escapeHtml(client.id)}</span>
            </button>
        `).join("");
    }

    function renderAccountList(role) {
        const rs = roleState(role.key);
        if (!rs.clientId) {
            return `<div class="picker-empty">Выберите клиента</div>`;
        }
        if (rs.loading) {
            return `<div class="picker-empty">Загрузка счетов...</div>`;
        }
        if (!rs.accounts.length) {
            return `<div class="picker-empty">Счетов нет</div>`;
        }
        return rs.accounts.map(account => `
            <button type="button"
                    class="picker-row picker-row--stacked${selectedClass(rs.accountId === account.id)}"
                    data-action="select-account"
                    data-role="${role.key}"
                    data-id="${account.id}">
                <span class="picker-row-main">${TM.escapeHtml(entityNumber(account))}</span>
                <span class="picker-row-sub">${TM.escapeHtml(entityDescription(account))}</span>
            </button>
        `).join("");
    }

    function renderCardList(role) {
        const rs = roleState(role.key);
        if (!rs.clientId) {
            return `<div class="picker-empty">Выберите клиента</div>`;
        }
        if (rs.loading) {
            return `<div class="picker-empty">Загрузка карт...</div>`;
        }
        if (!rs.cards.length) {
            return `<div class="picker-empty">Карт нет</div>`;
        }
        const accountsById = accountNumberById(role.key);
        return rs.cards.map(card => {
            const accountNumber = accountsById.get(card.account_id) || card.account_id;
            return `
                <button type="button"
                        class="picker-row picker-row--stacked${selectedClass(rs.cardId === card.id)}"
                        data-action="select-card"
                        data-role="${role.key}"
                        data-id="${card.id}">
                    <span class="picker-row-main">${TM.escapeHtml(entityNumber(card))}</span>
                    <span class="picker-row-sub">${TM.escapeHtml(entityDescription(card))}</span>
                    <span class="picker-row-meta">Счёт: ${TM.escapeHtml(accountNumber)}</span>
                </button>
            `;
        }).join("");
    }

    function renderRole(role) {
        const rs = roleState(role.key);
        return `
            <section class="role-panel" data-role-panel="${role.key}">
                <div class="role-panel__header">
                    <h3>${TM.escapeHtml(role.title)}</h3>
                    <span>${TM.escapeHtml(role.key)}</span>
                </div>
                <div class="picker-group">
                    <label for="${role.key}-client-search">Клиенты</label>
                    <input id="${role.key}-client-search"
                           class="picker-search"
                           type="search"
                           placeholder="Поиск"
                           value="${TM.escapeHtml(rs.clientQuery)}"
                           data-action="search-clients"
                           data-role="${role.key}">
                    <div class="picker-list"
                         role="listbox"
                         aria-label="${TM.escapeHtml(role.title)}: клиенты"
                         data-list-id="${role.key}:clients">
                        ${renderClientList(role)}
                    </div>
                </div>
                <div class="role-panel__choices">
                    <div class="picker-group">
                        <label>Счета</label>
                        <div class="picker-list picker-list--stacked"
                             role="listbox"
                             aria-label="${TM.escapeHtml(role.title)}: счета"
                             data-list-id="${role.key}:accounts">
                            ${renderAccountList(role)}
                        </div>
                    </div>
                    <div class="picker-group">
                        <label>Карты</label>
                        <div class="picker-list picker-list--stacked"
                             role="listbox"
                             aria-label="${TM.escapeHtml(role.title)}: карты"
                             data-list-id="${role.key}:cards">
                            ${renderCardList(role)}
                        </div>
                    </div>
                </div>
            </section>
        `;
    }

    function findInteractiveElement(focusState) {
        if (!focusState) return null;
        if (focusState.id) {
            return document.getElementById(focusState.id);
        }
        return Array.from(rolePanels.querySelectorAll("[data-action][data-role]")).find(element => (
            element.dataset.action === focusState.action
            && element.dataset.role === focusState.role
            && element.dataset.id === focusState.idValue
        ));
    }

    function captureRenderState() {
        const active = document.activeElement;
        let focus = null;
        if (active instanceof HTMLElement && rolePanels.contains(active)) {
            focus = {
                id: active.id || null,
                action: active.dataset.action || null,
                role: active.dataset.role || null,
                idValue: active.dataset.id || null,
                selectionStart: active instanceof HTMLInputElement ? active.selectionStart : null,
                selectionEnd: active instanceof HTMLInputElement ? active.selectionEnd : null,
            };
        }
        const scroll = new Map();
        for (const list of rolePanels.querySelectorAll(".picker-list[data-list-id]")) {
            scroll.set(list.dataset.listId, list.scrollTop);
        }
        return { focus, scroll };
    }

    function restoreRenderState(snapshot) {
        for (const list of rolePanels.querySelectorAll(".picker-list[data-list-id]")) {
            if (snapshot.scroll.has(list.dataset.listId)) {
                list.scrollTop = snapshot.scroll.get(list.dataset.listId);
            }
        }
        const target = findInteractiveElement(snapshot.focus);
        if (!target) return;
        try {
            target.focus({ preventScroll: true });
        } catch {
            target.focus();
        }
        if (
            target instanceof HTMLInputElement
            && snapshot.focus.selectionStart !== null
            && snapshot.focus.selectionEnd !== null
        ) {
            target.setSelectionRange(snapshot.focus.selectionStart, snapshot.focus.selectionEnd);
        }
    }

    function renderAll() {
        const snapshot = captureRenderState();
        rolePanels.innerHTML = roles.map(renderRole).join("");
        restoreRenderState(snapshot);
    }

    async function loadRoleEntities(role) {
        const rs = roleState(role.key);
        if (!rs.clientId) {
            rs.accounts = [];
            rs.cards = [];
            renderAll();
            return;
        }
        const token = Symbol(role.key);
        rs.loadToken = token;
        rs.loading = true;
        rs.accounts = [];
        rs.cards = [];
        renderAll();
        try {
            const clientId = encodeURIComponent(rs.clientId);
            const [accounts, cards] = await Promise.all([
                TM.api("GET", `/api/accounts?client_id=${clientId}`),
                TM.api("GET", `/api/cards?client_id=${clientId}`),
            ]);
            if (rs.loadToken !== token) return;
            rs.accounts = accounts;
            rs.cards = cards;
        } catch (e) {
            if (rs.loadToken === token) {
                TM.toast("Не удалось загрузить счета и карты: " + e.message, "error");
            }
        } finally {
            if (rs.loadToken === token) {
                rs.loading = false;
                renderAll();
            }
        }
    }

    function setClient(roleKey, clientId) {
        const role = roleByKey.get(roleKey);
        if (!role) return;
        const rs = roleState(roleKey);
        rs.clientId = clientId;
        rs.accountId = null;
        rs.cardId = null;
        loadRoleEntities(role);
    }

    function toggleAccount(roleKey, accountId) {
        const rs = roleState(roleKey);
        rs.accountId = rs.accountId === accountId ? null : accountId;
        if (rs.accountId) {
            rs.cardId = null;
        }
        renderAll();
    }

    function toggleCard(roleKey, cardId) {
        const rs = roleState(roleKey);
        rs.cardId = rs.cardId === cardId ? null : cardId;
        if (rs.cardId) {
            rs.accountId = null;
        }
        renderAll();
    }

    function buildRequestBody() {
        const body = {};
        for (const role of roles) {
            const rs = roleState(role.key);
            body[`${role.requestPrefix}_client_id`] = rs.clientId || null;
            body[`${role.requestPrefix}_account_id`] = rs.accountId || null;
            body[`${role.requestPrefix}_card_id`] = rs.cardId || null;
        }
        return body;
    }

    rolePanels.addEventListener("input", event => {
        const target = event.target;
        if (!(target instanceof HTMLInputElement)) return;
        if (target.dataset.action !== "search-clients") return;
        if (!target.dataset.role) return;
        const rs = roleState(target.dataset.role);
        rs.clientQuery = target.value;
        renderAll();
    });

    rolePanels.addEventListener("click", event => {
        if (!(event.target instanceof Element)) return;
        const button = event.target.closest("button[data-action]");
        if (!button) return;
        const action = button.dataset.action;
        const roleKey = button.dataset.role;
        const id = button.dataset.id;
        if (!roleKey || !id) return;
        if (action === "select-client") {
            setClient(roleKey, id);
        } else if (action === "select-account") {
            toggleAccount(roleKey, id);
        } else if (action === "select-card") {
            toggleCard(roleKey, id);
        }
    });

    btnRender.addEventListener("click", async () => {
        unresolved.hidden = true;
        unresolved.innerHTML = "";
        try {
            lastResult = await TM.api("POST", `/api/templates/${T.id}/fill`, buildRequestBody());
            if (lastResult.html) {
                result.innerHTML = lastResult.html;
            } else {
                result.textContent = lastResult.content;
            }
            btnDownload.disabled = false;
            if (lastResult.unresolved && lastResult.unresolved.length) {
                unresolved.hidden = false;
                unresolved.innerHTML = `<strong>Не подставлено:</strong> ${lastResult.unresolved.map(TM.escapeHtml).join(", ")}`;
            }
        } catch (e) {
            TM.toast(e.message, "error");
        }
    });

    btnDownload.addEventListener("click", () => {
        if (!lastResult) return;
        const ext = lastResult.format === "xml" ? "xml" : "json";
        const blob = new Blob([lastResult.content], {
            type: ext === "xml" ? "application/xml" : "application/json",
        });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `filled-${Date.now()}.${ext}`;
        document.body.appendChild(a);
        a.click();
        URL.revokeObjectURL(a.href);
        a.remove();
    });

    renderAll();
    try {
        clients = await TM.api("GET", "/api/clients");
        clientsLoading = false;
        renderAll();
    } catch (e) {
        clientsLoading = false;
        renderAll();
        TM.toast("Не удалось загрузить клиентов: " + e.message, "error");
    }
})();
