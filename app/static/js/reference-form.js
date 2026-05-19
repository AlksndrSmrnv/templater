(async function () {
    const PAGE = window.PAGE;
    const entityType = PAGE.entityType;
    const valueId = PAGE.valueId;
    const form = document.getElementById("ref-form");
    const fieldsHost = document.getElementById("form-fields");
    const errorsBox = document.getElementById("form-errors");
    const codeInput = document.getElementById("code");
    const nameInput = document.getElementById("name");
    const descInput = document.getElementById("description");
    const deleteBtn = document.getElementById("btn-delete");

    let schema = [];
    let item = { entity_type: entityType, code: "", name: "", description: "", attributes: {} };

    function attrInput(def, value) {
        const id = "attr-" + def.name.replace(/[^a-z0-9_]/gi, "_");
        const safe = TM.escapeHtml(value ?? "");
        let control = `<input type="text" id="${id}" value="${safe}">`;
        if (def.data_type === "text") control = `<textarea id="${id}" rows="3">${safe}</textarea>`;
        if (def.data_type === "int") control = `<input type="number" step="1" id="${id}" value="${safe}">`;
        if (def.data_type === "number") control = `<input type="number" step="any" id="${id}" value="${safe}">`;
        if (def.data_type === "bool") control = `<input type="checkbox" id="${id}" ${value ? "checked" : ""}>`;
        if (def.data_type === "date") control = `<input type="date" id="${id}" value="${safe}">`;
        if (def.data_type === "enum") {
            const opts = ['<option value="">—</option>'].concat(((def.options && def.options.values) || []).map(v =>
                `<option value="${TM.escapeHtml(v)}" ${value === v ? "selected" : ""}>${TM.escapeHtml(v)}</option>`));
            control = `<select id="${id}">${opts.join("")}</select>`;
        }
        return `<div class="form-section">
            <label for="${id}">${TM.escapeHtml(def.label)}${def.is_required ? '<span class="required-mark">*</span>' : ""}</label>
            ${control}
        </div>`;
    }

    async function build() {
        schema = await TM.api("GET", `/api/attribute-schema/${entityType}?include_deprecated=false`);
        if (valueId) {
            const fetched = await TM.api("GET", `/api/references/${entityType}/${valueId}`);
            item = Object.assign(item, fetched);
            codeInput.value = item.code;
            nameInput.value = item.name;
            descInput.value = item.description || "";
        }
        fieldsHost.innerHTML = schema.map(d => attrInput(d, item.attributes?.[d.name])).join("");
    }

    function readForm() {
        const attrs = {};
        for (const def of schema) {
            const id = "attr-" + def.name.replace(/[^a-z0-9_]/gi, "_");
            const el = document.getElementById(id);
            if (!el) continue;
            if (def.data_type === "bool") attrs[def.name] = el.checked;
            else if (el.value !== "") attrs[def.name] = el.value;
        }
        return { code: codeInput.value, name: nameInput.value, description: descInput.value, attributes: attrs };
    }

    form.addEventListener("submit", async e => {
        e.preventDefault();
        errorsBox.hidden = true; errorsBox.innerHTML = "";
        const data = readForm();
        try {
            if (valueId) {
                await TM.api("PUT", `/api/references/${entityType}/${valueId}`, data);
            } else {
                data.entity_type = entityType;
                await TM.api("POST", `/api/references/${entityType}`, data);
            }
            window.location.href = `/references/${entityType}`;
        } catch (err) {
            errorsBox.hidden = false;
            errorsBox.innerHTML = `<strong>${TM.escapeHtml(err.message)}</strong>`;
            TM.toast(err.message, "error");
        }
    });

    if (deleteBtn) deleteBtn.addEventListener("click", async () => {
        if (!TM.confirm("Удалить запись?")) return;
        try {
            await TM.api("DELETE", `/api/references/${entityType}/${valueId}`);
            window.location.href = `/references/${entityType}`;
        } catch (e) { TM.toast(e.message, "error"); }
    });

    try { await build(); }
    catch (e) { fieldsHost.innerHTML = `<div class="errors">Ошибка: ${TM.escapeHtml(e.message)}</div>`; }
})();
