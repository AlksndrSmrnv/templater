// Interactive placeholder editor. Hover/click on a highlighted span → searchable dropdown.

(async function () {
    const T = window.TEMPLATE;
    const code = document.getElementById("template-code");
    const saveBtn = document.getElementById("btn-save");
    const regenBtn = document.getElementById("btn-regenerate");
    const delBtn = document.getElementById("btn-delete");
    const descInput = document.getElementById("description");
    let placeholders = (T.placeholders || []).map(p => ({ ...p }));
    let catalog = [];
    let dropdown = null;

    function refreshSpans() {
        code.querySelectorAll(".placeholder").forEach(span => {
            const idx = parseInt(span.dataset.idx, 10);
            const ph = placeholders[idx];
            if (!ph) return;
            const value = ph.mode === "mapped" ? ph.value : ph.original;
            // for JSON, the surrounding quotes are outside the span — only the text inside changes
            span.textContent = value;
            span.classList.remove("mapped", "literal");
            span.classList.add(ph.mode);
        });
    }

    function closeDropdown() {
        if (dropdown) { dropdown.remove(); dropdown = null; }
        document.removeEventListener("click", outsideClickListener, true);
    }

    function outsideClickListener(e) {
        if (dropdown && !dropdown.contains(e.target) && !e.target.classList.contains("placeholder")) {
            closeDropdown();
        }
    }

    function openDropdown(span) {
        closeDropdown();
        const idx = parseInt(span.dataset.idx, 10);
        const ph = placeholders[idx];
        if (!ph) return;
        dropdown = document.createElement("div");
        dropdown.className = "dropdown";
        const rect = span.getBoundingClientRect();
        dropdown.style.top = (window.scrollY + rect.bottom + 4) + "px";
        dropdown.style.left = (window.scrollX + rect.left) + "px";
        dropdown.innerHTML = `
            <input type="text" placeholder="Поиск поля..." autocomplete="off">
            <ul class="dropdown-list">
                <li data-action="literal"><strong>Оставить исходное значение:</strong> ${TM.escapeHtml(ph.original)}</li>
            </ul>`;
        const ul = dropdown.querySelector("ul");
        const input = dropdown.querySelector("input");
        function renderOptions(filter) {
            const f = (filter || "").toLowerCase();
            ul.innerHTML = `<li data-action="literal"><strong>Оставить исходное значение:</strong> ${TM.escapeHtml(ph.original)}</li>`;
            for (const entry of catalog) {
                if (f && !(entry.path.toLowerCase().includes(f) || entry.label.toLowerCase().includes(f))) continue;
                const li = document.createElement("li");
                li.dataset.path = entry.path;
                li.innerHTML = `<span>${TM.escapeHtml(entry.label)}</span> <span class="path">${TM.escapeHtml(entry.path)}</span>`;
                ul.appendChild(li);
            }
            ul.querySelectorAll("li").forEach(li => {
                li.addEventListener("click", () => {
                    if (li.dataset.action === "literal") {
                        ph.mode = "literal";
                        ph.value = ph.original;
                    } else {
                        ph.mode = "mapped";
                        ph.value = `{{${li.dataset.path}}}`;
                        ph.suggestion = li.dataset.path;
                    }
                    refreshSpans();
                    closeDropdown();
                });
            });
        }
        renderOptions("");
        document.body.appendChild(dropdown);
        input.focus();
        input.addEventListener("input", e => renderOptions(e.target.value));
        setTimeout(() => document.addEventListener("click", outsideClickListener, true), 0);
    }

    code.addEventListener("click", e => {
        const span = e.target.closest(".placeholder");
        if (span) openDropdown(span);
    });

    saveBtn.addEventListener("click", async () => {
        try {
            await TM.api("PUT", `/api/templates/${T.id}`, {
                description: descInput.value,
                placeholders,
            });
            TM.toast("Сохранено", "success");
            const data = await TM.api("GET", `/api/templates/${T.id}/render`);
            code.innerHTML = data.html;
            placeholders = (data.placeholders || []).map(p => ({ ...p }));
        } catch (e) { TM.toast(e.message, "error"); }
    });

    if (regenBtn) regenBtn.addEventListener("click", async () => {
        regenBtn.disabled = true;
        regenBtn.innerHTML = '<span class="spinner"></span> Перегенерация...';
        try {
            const t = await TM.api("POST", `/api/templates/${T.id}/analyze`);
            placeholders = (t.placeholders || []).map(p => ({ ...p }));
            const data = await TM.api("GET", `/api/templates/${T.id}/render`);
            code.innerHTML = data.html;
            TM.toast("Шаблон перегенерирован", "success");
        } catch (e) { TM.toast(e.message, "error"); }
        finally {
            regenBtn.disabled = false;
            regenBtn.textContent = "Перегенерировать с LLM";
        }
    });

    delBtn.addEventListener("click", async () => {
        if (!TM.confirm("Удалить шаблон?")) return;
        try {
            await TM.api("DELETE", `/api/templates/${T.id}`);
            window.location.href = "/templates";
        } catch (e) { TM.toast(e.message, "error"); }
    });

    try { catalog = await TM.api("GET", "/api/templates/catalog"); }
    catch (e) { TM.toast("Не удалось загрузить каталог полей: " + e.message, "error"); }
})();
