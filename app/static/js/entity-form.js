// Universal form for clients/accounts/cards. Builds inputs from attribute_definitions.

(async function () {
    const PAGE = window.PAGE;
    const entityType = PAGE.entityType;
    const entityId = PAGE.entityId;

    const form = document.getElementById("entity-form");
    const fieldsHost = document.getElementById("form-fields");
    const errorsBox = document.getElementById("form-errors");
    const tagsInput = document.getElementById("tags-input");
    const tagsList = document.getElementById("tags-list");
    const descTextarea = document.getElementById("description");
    const deleteBtn = document.getElementById("btn-delete");

    let schema = [];
    let entity = { description: "", tags: [], attributes: {} };
    if (entityType === "account") entity.client_id = "";
    if (entityType === "card") entity.account_id = "";

    function renderTags() {
        tagsList.innerHTML = "";
        entity.tags.forEach((t, idx) => {
            const span = document.createElement("span");
            span.className = "tag";
            span.innerHTML = `${TM.escapeHtml(t)} <button type="button" data-idx="${idx}">×</button>`;
            tagsList.appendChild(span);
        });
        tagsList.querySelectorAll("button").forEach(b => {
            b.addEventListener("click", () => {
                entity.tags.splice(parseInt(b.dataset.idx, 10), 1);
                renderTags();
            });
        });
    }

    tagsInput.addEventListener("keydown", e => {
        if (e.key === "Enter") {
            e.preventDefault();
            const v = tagsInput.value.trim();
            if (v && !entity.tags.includes(v)) { entity.tags.push(v); renderTags(); }
            tagsInput.value = "";
        }
    });

    async function loadRefOptions(refEntity) {
        try {
            return await TM.api("GET", `/api/references/${refEntity}`);
        } catch (e) { return []; }
    }

    async function loadParentOptions() {
        if (entityType === "account") return await TM.api("GET", `/api/clients`);
        if (entityType === "card") return await TM.api("GET", `/api/accounts`);
        return null;
    }

    function attrInput(def, value) {
        const id = `attr-${def.name.replace(/[^a-z0-9_]/gi, "_")}`;
        const required = def.is_required ? `<span class="required-mark">*</span>` : "";
        const deprecated = def.is_deprecated ? `<span class="deprecated-marker">[устаревший]</span>` : "";
        let input = "";
        const safeVal = TM.escapeHtml(value ?? "");
        switch (def.data_type) {
            case "text":
                input = `<textarea id="${id}" rows="3">${safeVal}</textarea>`;
                break;
            case "int":
                input = `<input type="number" step="1" id="${id}" value="${safeVal}">`;
                break;
            case "number":
                input = `<input type="number" step="any" id="${id}" value="${safeVal}">`;
                break;
            case "bool":
                input = `<input type="checkbox" id="${id}" ${value ? "checked" : ""}>`;
                break;
            case "date":
                input = `<input type="date" id="${id}" value="${safeVal}">`;
                break;
            case "datetime":
                input = `<input type="datetime-local" id="${id}" value="${safeVal}">`;
                break;
            case "enum": {
                const values = (def.options && def.options.values) || [];
                const opts = ['<option value="">—</option>']
                    .concat(values.map(v => `<option value="${TM.escapeHtml(v)}" ${value === v ? "selected" : ""}>${TM.escapeHtml(v)}</option>`));
                input = `<select id="${id}">${opts.join("")}</select>`;
                break;
            }
            case "ref":
                input = `<select id="${id}" data-ref="${def.options?.ref_entity || ""}"><option value="">—</option></select>`;
                break;
            default:
                input = `<input type="text" id="${id}" value="${safeVal}">`;
        }
        return `<div class="form-section" data-attr="${def.name}">
            <label for="${id}">${TM.escapeHtml(def.label)} ${required}${deprecated}</label>
            ${input}
            ${def.description ? `<div class="muted" style="font-size:12px; margin-top:4px;">${TM.escapeHtml(def.description)}</div>` : ""}
        </div>`;
    }

    async function build() {
        schema = await TM.api("GET", `/api/attribute-schema/${entityType}?include_deprecated=true`);
        if (entityId) {
            const fetched = await TM.api("GET", `/api/${entityType}s/${entityId}`);
            entity = Object.assign(entity, fetched);
        }
        // parent select (for account/card)
        let parentHtml = "";
        if (entityType === "account") {
            const clients = await loadParentOptions();
            parentHtml = `<div class="form-section">
                <label>Клиент <span class="required-mark">*</span></label>
                <select id="parent-select" required>
                    <option value="">— выбрать —</option>
                    ${clients.map(c => `<option value="${c.id}" ${entity.client_id === c.id ? "selected" : ""}>${TM.escapeHtml(c.attributes?.fullName || c.id)}</option>`).join("")}
                </select>
            </div>`;
        } else if (entityType === "card") {
            const accounts = await loadParentOptions();
            parentHtml = `<div class="form-section">
                <label>Счёт <span class="required-mark">*</span></label>
                <select id="parent-select" required>
                    <option value="">— выбрать —</option>
                    ${accounts.map(a => `<option value="${a.id}" ${entity.account_id === a.id ? "selected" : ""}>${TM.escapeHtml(a.attributes?.number || a.id)}</option>`).join("")}
                </select>
            </div>`;
        }

        const visible = schema.filter(d => !d.is_deprecated || (entity.attributes && entity.attributes[d.name] !== undefined));
        fieldsHost.innerHTML = parentHtml + visible.map(d => attrInput(d, (entity.attributes || {})[d.name])).join("");

        // populate ref selects with reference values, then set current value from schema lookup
        for (const def of visible) {
            if (def.data_type !== "ref") continue;
            const id = "attr-" + def.name.replace(/[^a-z0-9_]/gi, "_");
            const sel = document.getElementById(id);
            if (!sel) continue;
            const refEntity = def.options?.ref_entity;
            if (!refEntity) continue;
            const items = await loadRefOptions(refEntity);
            sel.innerHTML = `<option value="">—</option>` + items.map(r =>
                `<option value="${r.id}">${TM.escapeHtml(r.name)}${r.code ? " (" + TM.escapeHtml(r.code) + ")" : ""}</option>`
            ).join("");
            const val = (entity.attributes || {})[def.name];
            if (val) sel.value = val;
        }
        descTextarea.value = entity.description || "";
        renderTags();
    }

    function readForm() {
        const data = { description: descTextarea.value, tags: entity.tags.slice(), attributes: {} };
        if (entityType === "account") {
            const ps = document.getElementById("parent-select");
            data.client_id = ps ? ps.value : entity.client_id;
        }
        if (entityType === "card") {
            const ps = document.getElementById("parent-select");
            data.account_id = ps ? ps.value : entity.account_id;
        }
        for (const def of schema) {
            const id = "attr-" + def.name.replace(/[^a-z0-9_]/gi, "_");
            const el = document.getElementById(id);
            if (!el) continue;
            if (def.data_type === "bool") { data.attributes[def.name] = el.checked; }
            else if (el.value === "") {
                // skip; missing means absence
            } else {
                data.attributes[def.name] = el.value;
            }
        }
        return data;
    }

    form.addEventListener("submit", async e => {
        e.preventDefault();
        errorsBox.hidden = true; errorsBox.innerHTML = "";
        const data = readForm();
        try {
            if (entityId) {
                await TM.api("PUT", `/api/${entityType}s/${entityId}`, data);
            } else {
                await TM.api("POST", `/api/${entityType}s`, data);
            }
            window.location.href = `/${entityType}s`;
        } catch (err) {
            errorsBox.hidden = false;
            const detailLines = Array.isArray(err.details) ? err.details.map(TM.escapeHtml).join("<br>") : "";
            errorsBox.innerHTML = `<strong>${TM.escapeHtml(err.message)}</strong>${detailLines ? "<br>" + detailLines : ""}`;
            TM.toast(err.message, "error");
        }
    });

    if (deleteBtn) {
        deleteBtn.addEventListener("click", async () => {
            if (!TM.confirm("Удалить запись?")) return;
            try {
                await TM.api("DELETE", `/api/${entityType}s/${entityId}`);
                window.location.href = `/${entityType}s`;
            } catch (e) { TM.toast(e.message, "error"); }
        });
    }

    try { await build(); }
    catch (e) {
        fieldsHost.innerHTML = `<div class="errors">Ошибка загрузки: ${TM.escapeHtml(e.message)}</div>`;
    }
})();
