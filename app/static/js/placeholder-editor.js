// Shared interactive placeholder editor for rendered template previews.

(function () {
    window.TM = window.TM || {};

    TM.mountPlaceholderEditor = function ({ codeEl, placeholders = [], catalog = [] }) {
        let currentPlaceholders = (placeholders || []).map(p => ({ ...p }));
        let currentCatalog = catalog || [];
        let dropdown = null;

        function refreshSpans() {
            codeEl.querySelectorAll(".placeholder").forEach(span => {
                const idx = parseInt(span.dataset.idx, 10);
                const ph = currentPlaceholders[idx];
                if (!ph) return;
                const value = ph.mode === "mapped" ? ph.value : ph.original;
                span.textContent = value;
                span.title = ph.location || "";
                span.classList.remove("mapped", "literal");
                span.classList.add(ph.mode);
            });
        }

        function closeDropdown() {
            if (dropdown) {
                dropdown.remove();
                dropdown = null;
            }
            document.removeEventListener("click", outsideClickListener, true);
        }

        function outsideClickListener(e) {
            const target = e.target;
            if (
                target instanceof Element
                && dropdown
                && !dropdown.contains(target)
                && !target.classList.contains("placeholder")
            ) {
                closeDropdown();
            }
        }

        function renderOptions(ul, ph, filter) {
            const f = (filter || "").toLowerCase();
            ul.innerHTML = `<li data-action="literal"><strong>Оставить исходное значение:</strong> ${TM.escapeHtml(ph.original)}</li>`;
            for (const entry of currentCatalog) {
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

        function openDropdown(span) {
            closeDropdown();
            const idx = parseInt(span.dataset.idx, 10);
            const ph = currentPlaceholders[idx];
            if (!ph) return;
            dropdown = document.createElement("div");
            dropdown.className = "dropdown";
            const rect = span.getBoundingClientRect();
            dropdown.style.top = (window.scrollY + rect.bottom + 4) + "px";
            dropdown.style.left = (window.scrollX + rect.left) + "px";
            dropdown.innerHTML = `
                <input type="text" placeholder="Поиск поля..." autocomplete="off">
                <ul class="dropdown-list"></ul>`;
            const ul = dropdown.querySelector("ul");
            const input = dropdown.querySelector("input");
            renderOptions(ul, ph, "");
            document.body.appendChild(dropdown);
            input.focus();
            input.addEventListener("input", e => renderOptions(ul, ph, e.target.value));
            setTimeout(() => document.addEventListener("click", outsideClickListener, true), 0);
        }

        function handleCodeClick(e) {
            if (!(e.target instanceof Element)) return;
            const span = e.target.closest(".placeholder");
            if (span) openDropdown(span);
        }

        codeEl.addEventListener("click", handleCodeClick);
        refreshSpans();

        return {
            getPlaceholders() {
                return currentPlaceholders.map(p => ({ ...p }));
            },
            setCatalog(nextCatalog) {
                currentCatalog = nextCatalog || [];
            },
            setPlaceholders(nextPlaceholders) {
                currentPlaceholders = (nextPlaceholders || []).map(p => ({ ...p }));
                refreshSpans();
            },
            refresh: refreshSpans,
            destroy() {
                closeDropdown();
                codeEl.removeEventListener("click", handleCodeClick);
            },
        };
    };
})();
