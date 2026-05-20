// Universal table view for clients/accounts/cards.

(async function () {
    const PAGE = window.PAGE;
    const entityType = PAGE.entityType;
    const tbody = document.getElementById("entities-body");
    const theadRow = document.getElementById("thead-row");
    const searchInput = document.getElementById("search");
    const selectAll = document.getElementById("select-all");
    const exportBtn = document.getElementById("btn-export");

    let schema = [];
    let items = [];
    let refsCache = {};   // ref_entity -> {id: name}
    let state = { sort: { key: "created_at", dir: "desc" }, filters: {}, search: "" };

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

    function render() {
        // header
        const active = schema.filter(d => !d.is_deprecated);
        const headers = [
            { key: "__select", label: "" },
            ...active.map(d => ({ key: d.name, label: d.label, def: d })),
            { key: "tags", label: "Теги" },
            { key: "created_at", label: "Создано" },
            { key: "__actions", label: "" },
        ];
        theadRow.innerHTML = "";
        for (const h of headers) {
            const th = document.createElement("th");
            if (h.key === "__select") {
                th.innerHTML = `<input type="checkbox" id="select-all-inline">`;
            } else if (h.key === "__actions") {
                th.textContent = "";
            } else {
                const sortIcon = state.sort.key === h.key ? (state.sort.dir === "asc" ? "▲" : "▼") : "↕";
                th.innerHTML = `<span>${TM.escapeHtml(h.label)}</span> <span class="sort-icon">${sortIcon}</span>`;
                th.addEventListener("click", () => setSort(h.key));
                if (h.def) {
                    const input = document.createElement("input");
                    input.type = "search"; input.placeholder = "фильтр";
                    input.style.cssText = "display:block; margin-top:4px; width:100%; padding:2px 6px; font-size:11px;";
                    input.value = state.filters[h.key] || "";
                    input.addEventListener("click", e => e.stopPropagation());
                    input.addEventListener("input", e => {
                        state.filters[h.key] = e.target.value;
                        render();
                    });
                    th.appendChild(input);
                }
            }
            theadRow.appendChild(th);
        }

        // body
        const filtered = applySort(applyFilters(items));
        if (filtered.length === 0) {
            tbody.innerHTML = `<tr><td colspan="${headers.length}" class="muted" style="text-align:center; padding:24px;">Нет данных</td></tr>`;
            return;
        }
        tbody.innerHTML = "";
        for (const row of filtered) {
            const tr = document.createElement("tr");
            tr.dataset.id = row.id;
            tr.innerHTML = `<td><input type="checkbox" class="row-select" value="${row.id}"></td>` +
                active.map(d => `<td>${fmtAttr(d, (row.attributes || {})[d.name])}</td>`).join("") +
                `<td>${(row.tags || []).map(t => `<span class="tag">${TM.escapeHtml(t)}</span>`).join(" ")}</td>` +
                `<td>${TM.formatDate(row.created_at)}</td>` +
                `<td class="row-actions">
                    <a class="btn" href="/${entityType}s/${row.id}/edit">Редактировать</a>
                    <button class="btn danger" data-action="delete" data-id="${row.id}">Удалить</button>
                </td>`;
            tbody.appendChild(tr);
        }

        tbody.querySelectorAll("[data-action=delete]").forEach(b => {
            b.addEventListener("click", async () => {
                if (!TM.confirm("Удалить запись?")) return;
                try {
                    await TM.api("DELETE", `/api/${entityType}s/${b.dataset.id}`);
                    items = items.filter(i => i.id !== b.dataset.id);
                    render();
                    TM.toast("Удалено", "success");
                } catch (e) {
                    TM.toast(e.message, "error");
                }
            });
        });
    }

    if (searchInput) searchInput.addEventListener("input", e => { state.search = e.target.value; render(); });
    if (selectAll) selectAll.addEventListener("change", e => {
        document.querySelectorAll(".row-select").forEach(cb => { cb.checked = e.target.checked; });
    });
    if (exportBtn) exportBtn.addEventListener("click", async () => {
        const ids = Array.from(document.querySelectorAll(".row-select:checked")).map(cb => cb.value);
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
        render();
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="20" style="color:var(--danger); padding:14px;">Ошибка: ${TM.escapeHtml(e.message)}</td></tr>`;
    }
})();
