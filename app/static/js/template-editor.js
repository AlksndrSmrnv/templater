// Template view actions: save placeholder edits, rerun LLM analysis, delete.

(async function () {
    const T = window.TEMPLATE;
    const code = document.getElementById("template-code");
    const saveBtn = document.getElementById("btn-save");
    const regenBtn = document.getElementById("btn-regenerate");
    const delBtn = document.getElementById("btn-delete");
    const descInput = document.getElementById("description");
    const editor = TM.mountPlaceholderEditor({
        codeEl: code,
        placeholders: T.placeholders || [],
    });

    saveBtn.addEventListener("click", async () => {
        try {
            await TM.api("PUT", `/api/templates/${T.id}`, {
                description: descInput.value,
                placeholders: editor.getPlaceholders(),
            });
            TM.toast("Сохранено", "success");
            const data = await TM.api("GET", `/api/templates/${T.id}/render`);
            code.innerHTML = data.html;
            editor.setPlaceholders(data.placeholders || []);
        } catch (e) { TM.toast(e.message, "error"); }
    });

    if (regenBtn) regenBtn.addEventListener("click", async () => {
        regenBtn.disabled = true;
        regenBtn.innerHTML = '<span class="spinner"></span> Перегенерация...';
        try {
            const t = await TM.api("POST", `/api/templates/${T.id}/analyze`);
            editor.setPlaceholders(t.placeholders || []);
            const data = await TM.api("GET", `/api/templates/${T.id}/render`);
            code.innerHTML = data.html;
            editor.setPlaceholders(data.placeholders || []);
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

    try { editor.setCatalog(await TM.api("GET", "/api/templates/catalog")); }
    catch (e) { TM.toast("Не удалось загрузить каталог полей: " + e.message, "error"); }
})();
