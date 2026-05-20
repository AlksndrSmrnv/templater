(async function () {
    const PAGE = window.PAGE;
    const entityType = PAGE.entityType;
    const tbody = document.getElementById("entities-body");
    const theadRow = document.getElementById("thead-row");
    const searchInput = document.getElementById("search");

    let schema = [];
    let items = [];
    let state = { sort: { key: "code", dir: "asc" }, search: "" };

    async function load() {
        schema = await TM.api("GET", `/api/attribute-schema/${entityType}?include_deprecated=false`);
        items = await TM.api("GET", `/api/references/${entityType}`);
    }

    function render() {
        const active = schema.filter(d => !d.is_deprecated);
        const headers = [
            { key: "code", label: "Код" },
            { key: "name", label: "Название" },
            ...active.map(d => ({ key: d.name, label: d.label, def: d })),
            { key: "__actions", label: "" },
        ];
        theadRow.innerHTML = "";
        for (const h of headers) {
            const th = document.createElement("th");
            const isSortable = h.key !== "__actions";
            const icon = isSortable ? (state.sort.key === h.key ? (state.sort.dir === "asc" ? "▲" : "▼") : "↕") : "";
            th.innerHTML = `<span>${TM.escapeHtml(h.label)}</span> <span class="sort-icon">${icon}</span>`;
            if (isSortable) th.addEventListener("click", () => {
                if (state.sort.key === h.key) state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
                else state.sort = { key: h.key, dir: "asc" };
                render();
            });
            theadRow.appendChild(th);
        }

        const search = state.search.toLowerCase();
        let filtered = items.filter(r => {
            if (!search) return true;
            return (r.code + " " + r.name + " " + (r.description || "")).toLowerCase().includes(search);
        });
        const def = schema.find(d => d.name === state.sort.key);
        filtered = filtered.sort((a, b) => {
            let va, vb;
            if (state.sort.key === "code" || state.sort.key === "name") { va = a[state.sort.key]; vb = b[state.sort.key]; }
            else if (def) { va = (a.attributes || {})[state.sort.key] || ""; vb = (b.attributes || {})[state.sort.key] || ""; }
            else { va = ""; vb = ""; }
            return state.sort.dir === "asc" ? (va > vb ? 1 : va < vb ? -1 : 0) : (va < vb ? 1 : va > vb ? -1 : 0);
        });

        if (filtered.length === 0) {
            tbody.innerHTML = `<tr><td colspan="${headers.length}" class="muted" style="text-align:center; padding:24px;">Нет данных</td></tr>`;
            return;
        }
        tbody.innerHTML = "";
        for (const row of filtered) {
            const tr = document.createElement("tr");
            tr.innerHTML = `<td>${TM.escapeHtml(row.code)}</td>` +
                `<td>${TM.escapeHtml(row.name)}</td>` +
                active.map(d => `<td>${TM.escapeHtml((row.attributes || {})[d.name] ?? "")}</td>`).join("") +
                `<td class="row-actions">
                    <a class="btn" href="/references/${entityType}/${row.id}/edit">Редактировать</a>
                    <button class="btn danger" data-id="${row.id}">Удалить</button>
                </td>`;
            tbody.appendChild(tr);
        }
        tbody.querySelectorAll("button[data-id]").forEach(b => {
            b.addEventListener("click", async () => {
                if (!TM.confirm("Удалить запись?")) return;
                try {
                    await TM.api("DELETE", `/api/references/${entityType}/${b.dataset.id}`);
                    items = items.filter(i => i.id !== b.dataset.id);
                    render();
                    TM.toast("Удалено", "success");
                } catch (e) { TM.toast(e.message, "error"); }
            });
        });
    }

    if (searchInput) searchInput.addEventListener("input", e => { state.search = e.target.value; render(); });

    try { await load(); render(); }
    catch (e) { tbody.innerHTML = `<tr><td colspan="20" style="color:var(--danger);padding:14px">Ошибка: ${TM.escapeHtml(e.message)}</td></tr>`; }
})();
