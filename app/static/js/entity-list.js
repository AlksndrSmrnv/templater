// Universal table view for clients/accounts/cards.

(async function () {
    const PAGE = window.PAGE;
    const entityType = PAGE.entityType;
    const tbody = document.getElementById("entities-body");
    const theadRow = document.getElementById("thead-row");
    const searchInput = document.getElementById("search");
    const exportBtn = document.getElementById("btn-export");
    const tableMeta = document.getElementById("table-meta");
    const detailDrawer = document.getElementById("detail-drawer");
    const drawerBody = document.getElementById("drawer-body");
    const drawerTitle = document.getElementById("drawer-title");
    const drawerEdit = document.getElementById("drawer-edit");
    const drawerDelete = document.getElementById("drawer-delete");
    const drawerClose = document.getElementById("drawer-close");

    let schema = [];
    let items = [];
    let refsCache = {};   // ref_entity -> {id: name}
    let clientsById = {};
    let accountsById = {};
    let cardsById = {};
    let accountsByClient = {};
    let cardsByAccount = {};
    let state = { sort: { key: "created_at", dir: "desc" }, filters: {}, search: "" };
    const selectedIds = new Set();
    let openId = null;
    let filteredCount = 0;
    let filteredIds = [];

    async function loadSchema() {
        schema = await TM.api("GET", `/api/attribute-schema/${entityType}`);
    }

    async function loadRefs() {
        const refTypes = new Set(schema.filter(d => d.data_type === "ref").map(d => d.options?.ref_entity).filter(Boolean));
        for (const t of refTypes) {
            if (refsCache[t]) continue;
            try {
                const list = await TM.api("GET", `/api/references/${t}`);
                const map = {};
                for (const r of list) map[r.id] = r.name + (r.code ? ` (${r.code})` : "");
                refsCache[t] = map;
            } catch (e) { refsCache[t] = {}; }
        }
    }

    async function loadItems() {
        const url = `/api/${entityType}s`;
        items = await TM.api("GET", url);
    }

    async function loadRelations() {
        const [clients, accounts, cards] = await Promise.all([
            entityType === "client" ? Promise.resolve(items) : TM.api("GET", "/api/clients"),
            entityType === "account" ? Promise.resolve(items) : TM.api("GET", "/api/accounts"),
            entityType === "card" ? Promise.resolve(items) : TM.api("GET", "/api/cards"),
        ]);

        clientsById = indexById(clients);
        accountsById = indexById(accounts);
        cardsById = indexById(cards);
        accountsByClient = groupBy(accounts, "client_id");
        cardsByAccount = groupBy(cards, "account_id");
    }

    function fmtAttr(def, value) {
        if (value === null || value === undefined || value === "") return "";
        if (def.data_type === "ref") {
            const ref = def.options?.ref_entity;
            return TM.escapeHtml((refsCache[ref] || {})[value] || value);
        }
        if (def.data_type === "bool") return value ? "✓" : "✗";
        return TM.escapeHtml(value);
    }

    function applyFilters(rows) {
        const search = state.search.toLowerCase();
        return rows.filter(row => {
            if (search) {
                const blob = JSON.stringify(row.attributes || {}).toLowerCase()
                    + " " + (row.tags || []).join(" ").toLowerCase()
                    + " " + (row.description || "").toLowerCase();
                if (!blob.includes(search)) return false;
            }
            for (const [name, val] of Object.entries(state.filters)) {
                if (!val) continue;
                const fieldVal = row.attributes && row.attributes[name];
                if (fieldVal === undefined || fieldVal === null) return false;
                if (!String(fieldVal).toLowerCase().includes(val.toLowerCase())) return false;
            }
            return true;
        });
    }

    function applySort(rows) {
        const { key, dir } = state.sort;
        const def = schema.find(d => d.name === key);
        const sorted = rows.slice().sort((a, b) => {
            let va, vb;
            if (key === "created_at" || key === "updated_at") { va = a[key]; vb = b[key]; }
            else if (def) { va = (a.attributes || {})[key]; vb = (b.attributes || {})[key]; }
            else { va = a[key]; vb = b[key]; }
            if (va === undefined || va === null) va = "";
            if (vb === undefined || vb === null) vb = "";
            if (va < vb) return dir === "asc" ? -1 : 1;
            if (va > vb) return dir === "asc" ? 1 : -1;
            return 0;
        });
        return sorted;
    }

    function setSort(key) {
        if (state.sort.key === key) {
            state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
        } else {
            state.sort = { key, dir: "asc" };
        }
        render();
    }

    function normalizeId(id) {
        return String(id);
    }

    function indexById(rows) {
        const map = {};
        for (const row of rows || []) {
            if (row.id !== null && row.id !== undefined) map[normalizeId(row.id)] = row;
        }
        return map;
    }

    function groupBy(rows, key) {
        const map = {};
        for (const row of rows || []) {
            const value = row[key];
            if (value === null || value === undefined) continue;
            const groupKey = normalizeId(value);
            if (!map[groupKey]) map[groupKey] = [];
            map[groupKey].push(row);
        }
        return map;
    }

    function activeSchema() {
        return schema.filter(d => !d.is_deprecated);
    }

    function relationColumns() {
        if (entityType === "client") {
            return [
                { key: "__rel_accounts", label: "Счета", noSort: true },
                { key: "__rel_cards", label: "Карты", noSort: true },
            ];
        }
        if (entityType === "account") {
            return [
                { key: "__rel_client", label: "Клиент", noSort: true },
                { key: "__rel_cards", label: "Карты", noSort: true },
            ];
        }
        if (entityType === "card") {
            return [
                { key: "__rel_client", label: "Клиент", noSort: true },
                { key: "__rel_account", label: "Счёт", noSort: true },
            ];
        }
        return [];
    }

    function relationLinks(type, rows) {
        const list = (rows || []).filter(Boolean);
        if (!list.length) return "—";
        const links = list.map(row => TM.entityLink(type, row.id, TM.entityLabel(type, row)));
        return `<div class="relation-links">${links.join("")}</div>`;
    }

    function relationCell(row, key) {
        const rowId = normalizeId(row.id);
        if (key === "__rel_accounts") {
            return relationLinks("account", accountsByClient[rowId]);
        }
        if (key === "__rel_cards" && entityType === "client") {
            const accounts = accountsByClient[rowId] || [];
            const cards = accounts.flatMap(account => cardsByAccount[normalizeId(account.id)] || []);
            return relationLinks("card", cards);
        }
        if (key === "__rel_cards") {
            return relationLinks("card", cardsByAccount[rowId]);
        }
        if (key === "__rel_client") {
            const clientId = entityType === "card"
                ? accountsById[normalizeId(row.account_id)]?.client_id
                : row.client_id;
            return relationLinks("client", [clientsById[normalizeId(clientId)]]);
        }
        if (key === "__rel_account") {
            return relationLinks("account", [accountsById[normalizeId(row.account_id)]]);
        }
        return "—";
    }

    function updateMeta() {
        if (!tableMeta) return;
        const parts = [`Записей: ${items.length}`];
        const hasActiveFilters = Boolean(state.search.trim()) ||
            Object.values(state.filters).some(v => Boolean(String(v || "").trim()));
        if (hasActiveFilters) parts.push(`Показано: ${filteredCount}`);
        if (selectedIds.size > 0) parts.push(`Выбрано: ${selectedIds.size}`);
        tableMeta.textContent = parts.join(" · ");
    }

    function syncSelectAll() {
        const selectAll = document.getElementById("select-all");
        if (!selectAll) return;
        const checkboxes = Array.from(tbody.querySelectorAll(".row-select"));
        const checkedCount = checkboxes.filter(cb => selectedIds.has(cb.value)).length;
        selectAll.checked = checkboxes.length > 0 && checkedCount === checkboxes.length;
        selectAll.indeterminate = checkedCount > 0 && checkedCount < checkboxes.length;
        selectAll.disabled = checkboxes.length === 0;
    }

    function syncActiveRows() {
        tbody.querySelectorAll("tr[data-id]").forEach(tr => {
            tr.classList.toggle("active", tr.dataset.id === openId);
        });
    }

    function toggleRow(id, on, tr) {
        const rowId = normalizeId(id);
        if (on) selectedIds.add(rowId);
        else selectedIds.delete(rowId);

        if (tr) {
            tr.classList.toggle("selected", on);
            const checkbox = tr.querySelector(".row-select");
            if (checkbox) checkbox.checked = on;
        }

        updateMeta();
        syncSelectAll();
    }

    function restoreFilterFocus(focusFilter) {
        if (!focusFilter) return;
        const input = Array.from(theadRow.querySelectorAll("input[data-filter-key]"))
            .find(el => el.dataset.filterKey === focusFilter.key);
        if (!input) return;

        input.focus({ preventScroll: true });
        if (typeof focusFilter.selectionStart === "number" && typeof focusFilter.selectionEnd === "number") {
            input.setSelectionRange(focusFilter.selectionStart, focusFilter.selectionEnd);
        }
    }

    function detailField(label, valueHtml) {
        return `<div class="detail-field">
            <div class="detail-label">${TM.escapeHtml(label)}</div>
            <div class="detail-value">${valueHtml || "—"}</div>
        </div>`;
    }

    function buildDetailBody(row) {
        const attrs = row.attributes || {};
        const fields = activeSchema().map(def => detailField(def.label, fmtAttr(def, attrs[def.name]) || "—"));
        const description = row.description ? TM.escapeHtml(row.description) : "—";
        const relationFields = relationColumns().map(column => detailField(column.label, relationCell(row, column.key)));
        const tags = (row.tags || []).length
            ? row.tags.map(t => `<span class="tag">${TM.escapeHtml(t)}</span>`).join(" ")
            : "—";
        const createdAt = TM.escapeHtml(TM.formatDate(row.created_at)) || "—";
        const updatedAt = TM.escapeHtml(TM.formatDate(row.updated_at)) || "—";

        fields.unshift(detailField("Описание", description), ...relationFields);
        fields.push(detailField("Теги", tags));
        fields.push(detailField("Создано", createdAt));
        fields.push(detailField("Обновлено", updatedAt));
        return fields.join("");
    }

    function openDetail(id) {
        const rowId = normalizeId(id);
        const row = items.find(i => normalizeId(i.id) === rowId);
        if (!row || !detailDrawer || !drawerBody || !drawerEdit) return;

        openId = rowId;
        if (drawerTitle) drawerTitle.textContent = "Запись";
        drawerBody.innerHTML = buildDetailBody(row);
        drawerEdit.href = `/${entityType}s/${encodeURIComponent(rowId)}/edit`;
        detailDrawer.classList.add("open");
        detailDrawer.setAttribute("aria-hidden", "false");
        syncActiveRows();
    }

    function closeDetail() {
        openId = null;
        if (detailDrawer) {
            detailDrawer.classList.remove("open");
            detailDrawer.setAttribute("aria-hidden", "true");
        }
        syncActiveRows();
    }

    function render(focusFilter) {
        // header
        const active = activeSchema();
        const headers = [
            { key: "__select", label: "" },
            { key: "description", label: "Описание" },
            ...active.map(d => ({ key: d.name, label: d.label, def: d })),
            ...relationColumns(),
            { key: "tags", label: "Теги" },
            { key: "created_at", label: "Создано" },
        ];
        theadRow.innerHTML = "";
        for (const h of headers) {
            const th = document.createElement("th");
            if (h.key === "__select") {
                th.classList.add("no-sort");
                th.innerHTML = `<input type="checkbox" id="select-all" aria-label="Выбрать все">`;
            } else {
                const sortable = !h.noSort;
                if (sortable) {
                    const sortIcon = state.sort.key === h.key ? (state.sort.dir === "asc" ? "▲" : "▼") : "↕";
                    th.innerHTML = `<span>${TM.escapeHtml(h.label)}</span> <span class="sort-icon">${sortIcon}</span>`;
                    th.addEventListener("click", () => setSort(h.key));
                } else {
                    th.classList.add("no-sort");
                    th.innerHTML = `<span>${TM.escapeHtml(h.label)}</span>`;
                }
                if (h.def) {
                    const input = document.createElement("input");
                    input.type = "search"; input.placeholder = "фильтр";
                    input.style.cssText = "display:block; margin-top:4px; width:100%; padding:2px 6px; font-size:11px;";
                    input.value = state.filters[h.key] || "";
                    input.dataset.filterKey = h.key;
                    input.addEventListener("click", e => e.stopPropagation());
                    input.addEventListener("input", e => {
                        state.filters[h.key] = e.target.value;
                        render({
                            key: h.key,
                            selectionStart: e.target.selectionStart,
                            selectionEnd: e.target.selectionEnd,
                        });
                    });
                    th.appendChild(input);
                }
            }
            theadRow.appendChild(th);
        }

        // body
        const filtered = applySort(applyFilters(items));
        filteredCount = filtered.length;
        filteredIds = filtered.map(row => normalizeId(row.id));
        if (filtered.length === 0) {
            tbody.innerHTML = `<tr><td colspan="${headers.length}" class="muted" style="text-align:center; padding:24px;">Нет данных</td></tr>`;
            updateMeta();
            syncSelectAll();
            restoreFilterFocus(focusFilter);
            return;
        }
        tbody.innerHTML = "";
        for (const row of filtered) {
            const rowId = normalizeId(row.id);
            const isSelected = selectedIds.has(rowId);
            const tr = document.createElement("tr");
            tr.dataset.id = rowId;
            tr.classList.toggle("selected", isSelected);
            tr.classList.toggle("active", rowId === openId);
            tr.innerHTML = `<td><input type="checkbox" class="row-select" value="${TM.escapeHtml(rowId)}" ${isSelected ? "checked" : ""} aria-label="Выбрать запись"></td>` +
                `<td class="col-description"><span class="description-text">${TM.escapeHtml(row.description || "")}</span></td>` +
                active.map(d => `<td>${fmtAttr(d, (row.attributes || {})[d.name])}</td>`).join("") +
                relationColumns().map(column => `<td>${relationCell(row, column.key)}</td>`).join("") +
                `<td>${(row.tags || []).map(t => `<span class="tag">${TM.escapeHtml(t)}</span>`).join(" ")}</td>` +
                `<td>${TM.escapeHtml(TM.formatDate(row.created_at))}</td>`;
            tbody.appendChild(tr);
        }

        updateMeta();
        syncSelectAll();
        restoreFilterFocus(focusFilter);
    }

    if (searchInput) searchInput.addEventListener("input", e => { state.search = e.target.value; render(); });
    tbody.addEventListener("click", e => {
        if (!(e.target instanceof Element)) return;
        if (e.target.closest("a")) return;
        const checkbox = e.target.closest(".row-select");
        if (checkbox) {
            e.stopPropagation();
            toggleRow(checkbox.value, checkbox.checked, checkbox.closest("tr"));
            return;
        }

        const tr = e.target.closest("tr[data-id]");
        if (tr && tbody.contains(tr)) openDetail(tr.dataset.id);
    });
    theadRow.addEventListener("change", e => {
        if (!(e.target instanceof HTMLInputElement) || e.target.id !== "select-all") return;
        const on = e.target.checked;
        filteredIds.forEach(id => {
            if (on) selectedIds.add(id);
            else selectedIds.delete(id);
        });
        tbody.querySelectorAll("tr[data-id]").forEach(tr => {
            const selected = selectedIds.has(tr.dataset.id);
            tr.classList.toggle("selected", selected);
            const checkbox = tr.querySelector(".row-select");
            if (checkbox) checkbox.checked = selected;
        });
        updateMeta();
        syncSelectAll();
    });
    if (drawerClose) drawerClose.addEventListener("click", closeDetail);
    document.addEventListener("keydown", e => {
        if (e.key === "Escape" && openId) closeDetail();
    });
    if (drawerDelete) drawerDelete.addEventListener("click", async () => {
        if (!openId) return;
        if (!TM.confirm("Удалить запись?")) return;
        const deleteId = openId;
        try {
            await TM.api("DELETE", `/api/${entityType}s/${encodeURIComponent(deleteId)}`);
            items = items.filter(i => normalizeId(i.id) !== deleteId);
            selectedIds.delete(deleteId);
            closeDetail();
            render();
            TM.toast("Удалено", "success");
        } catch (e) {
            TM.toast(e.message, "error");
        }
    });
    if (exportBtn) exportBtn.addEventListener("click", async () => {
        const ids = Array.from(selectedIds);
        if (ids.length === 0) { TM.toast("Выберите хотя бы одну запись", "error"); return; }
        const payload = { [entityType + "s"]: ids };
        const res = await fetch("/api/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        if (!res.ok) { TM.toast("Ошибка экспорта", "error"); return; }
        const blob = await res.blob();
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `tm-export-${entityType}-${Date.now()}.json`;
        document.body.appendChild(a); a.click(); a.remove();
    });

    try {
        await loadSchema();
        await loadRefs();
        await loadItems();
        await loadRelations();
        render();
        const initialOpenId = new URLSearchParams(window.location.search).get("open");
        if (initialOpenId) openDetail(initialOpenId);
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="20" style="color:var(--danger); padding:14px;">Ошибка: ${TM.escapeHtml(e.message)}</td></tr>`;
    }
})();
