/*
 * Alpine component for the «Цепочка запросов» panel (inline in the «Заполненные
 * шаблоны» workspace and on the standalone chain page).
 *
 * Steps are seeded from the server (DB-persisted) and each carries a
 * server-rendered, coloured `body_html`: blue = dynamic-by-default token, green
 * = filled with test data, purple = a `{{ $N.path }}` reference to an earlier
 * step's response, white = untouched literal. Every value is a clickable span
 * (`data-location`); clicking it opens a picker of the previous steps' response
 * fields and binds the chosen one as a reference (server replaces the leaf and
 * returns the refreshed body + markup, updated in place so other steps' sent
 * responses survive). Reordering and removal autosave via fetch; structural
 * *adds* are HTMX-driven (the whole panel re-renders from the server).
 *
 * Sending is a STUB — no real network request is made: the server seam echoes
 * the step's (hidden) example response back so later steps can pull fields from
 * it. The resolution/highlight/encoding helpers are ported from the (reverted)
 * PR #92 send page.
 *
 * Defined on window so HTMX-swapped panels can reference it without a per-swap
 * <script> race; loaded once at page level.
 */
window.chainPanel = function (config) {
    const base = '/templater/filled-templates-htmx/chains/';
    // Kept clear when flipping the popover upward. TOPBAR_H mirrors
    // .topbar { height } and FLIP_GAP the .chain-field-popover--above translateY
    // offset — both in app.css; keep in sync if those change.
    const TOPBAR_H = 64;
    const FLIP_GAP = 4;
    const emptyPicker = () => ({
        stepIdx: null, location: null, isRef: false, sourceStep: null,
        paths: [], filtered: [], query: '', anchor: { top: 0, left: 0, above: false },
    });
    return {
        chainId: config.chainId,
        executeUrl: config.executeUrl,
        steps: [],
        pickerOpen: false,
        pickerSearch: '',
        running: false,
        // Field-binding picker, anchored to the clicked body field. `paths` is
        // the source step's full leaf list; `filtered` is `paths` narrowed by
        // `query` (recomputed only on change, not per render).
        picker: emptyPicker(),

        init() {
            // Seed run-time/ephemeral fields onto the server-provided steps.
            this.steps = (config.steps || []).map((s) => ({
                id: s.id,
                name: s.name,
                method: s.method || '',
                url: s.url || '',
                headers: s.headers || [],
                format: s.format || 'json',
                body: s.body || '',
                bodyHtml: s.body_html || '',
                mockResponse: s.mock_response || '',
                collapsed: true,
                sending: false,
                error: '',
                response: null,
                resolvedRequestHtml: null,
                resolvedRefs: false,        // resolved body contains purple references
                resolvedUnresolved: false,  // ...and some couldn't be resolved (red)
            }));
        },

        // ---------- picker (client-side filter of server-rendered options) ----------
        matchPicker(name) {
            const q = this.pickerSearch.trim().toLowerCase();
            return !q || String(name || '').toLowerCase().includes(q);
        },

        // ---------- persistence ----------
        async persistOrder() {
            const order = this.steps.map((s) => s.id).join(',');
            try {
                const r = await fetch(base + this.chainId + '/steps/reorder', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({ order: order }),
                });
                return r.ok;
            } catch (e) { return false; }
        },
        refreshTree() {
            // Update the «N шагов» badge in the left tree after add/remove.
            if (window.htmx) window.htmx.trigger(document.body, 'refresh-filled-tree');
        },

        // ---------- step management ----------
        async removeStep(idx) {
            const step = this.steps[idx];
            if (!step) return;
            if (!confirm('Удалить шаг из цепочки?')) return;
            // Persist first, then update the UI — a failed delete must not leave
            // the step gone locally while it still exists on the server.
            let ok = false;
            try {
                const r = await fetch(base + this.chainId + '/steps/' + step.id, { method: 'DELETE' });
                ok = r.ok;
            } catch (e) { ok = false; }
            if (!ok) { this.toast('Не удалось удалить шаг', 'error'); return; }
            const pos = this.steps.findIndex((s) => s.id === step.id);
            if (pos !== -1) this.steps.splice(pos, 1);
            this.closePicker();
            this.refreshTree();
            this.toast('Шаг удалён');
        },
        async moveStep(idx, delta) {
            const j = idx + delta;
            if (j < 0 || j >= this.steps.length) return;
            const prev = this.steps.slice();  // snapshot for rollback
            const [s] = this.steps.splice(idx, 1);
            this.steps.splice(j, 0, s);
            // Indices shift on reorder, so a picker would point at the wrong
            // step — close it.
            this.closePicker();
            if (!await this.persistOrder()) {
                this.steps = prev;  // server rejected — restore the old order
                this.toast('Не удалось изменить порядок', 'error');
            }
        },

        // ---------- dependencies / prior steps ----------
        // Prior step numbers (1-based) a step references via {{ $N.path }} tokens
        // in its body — drives «зависит от шага N». Only steps before this one
        // (1..idx) are real dependencies; a self/forward reference can't resolve.
        stepDeps(idx) {
            const body = (this.steps[idx] && this.steps[idx].body) || '';
            const re = /\{\{\s*\$(\d+)\.[^}\s]+\s*\}\}/g;
            const out = [];
            let m;
            while ((m = re.exec(body)) !== null) {
                const n = parseInt(m[1], 10);
                if (n >= 1 && n <= idx && !out.includes(n)) out.push(n);
            }
            return out.sort((a, b) => a - b);
        },
        // True when at least one step references an earlier step's response —
        // drives the «Зависимости сообщений» overview (which recomputes from the
        // reactive `steps`, so it stays correct after bind/unbind/reorder/remove).
        hasAnyDeps() {
            return this.steps.some((_, i) => this.stepDeps(i).length > 0);
        },
        priorSteps(idx) {
            return this.steps.slice(0, idx).map((s, i) => ({
                num: i + 1, name: s.name, sent: !!s.response,
            }));
        },

        // ---------- click-to-bind ----------
        // A click anywhere in the rendered body: if it landed on a field span,
        // open the binding picker for that leaf.
        onFieldClick(ev, idx) {
            const span = ev.target.closest && ev.target.closest('.placeholder[data-location]');
            if (!span) return;
            // The first step has no previous response to pull from — its fields
            // aren't bindable (and the cursor reflects that), so do nothing.
            if (idx === 0) return;
            // Re-clicking another field of the same step just re-anchors: keep
            // the chosen source step / field search so the user doesn't drop
            // back to stage 1.
            const same = this.picker.stepIdx === idx;
            this.picker.stepIdx = idx;
            this.picker.location = span.dataset.location;
            this.picker.isRef = span.classList.contains('reference');
            this.picker.anchor = this.anchorFor(span);
            if (same) {
                // Re-anchor only: keep the chosen source step / search, but
                // refresh its field list in case the source was (re)sent since.
                if (this.picker.sourceStep !== null) {
                    this.picker.paths = this.loadSourcePaths(this.picker.sourceStep);
                    this.applyFieldFilter();
                }
            } else {
                this.picker.sourceStep = null;
                this.picker.paths = [];
                this.picker.filtered = [];
                this.picker.query = '';
            }
        },
        // Position the popover next to the clicked field, relative to the body
        // wrapper. Clamps to the body width (no right-edge spill) and flips above
        // the field when there isn't room below — but only if the flipped popover
        // clears the sticky topbar, otherwise it stays below.
        anchorFor(span) {
            const wrap = span.closest('.chain-body-wrap');
            if (!wrap) return { top: 0, left: 0, above: false };
            const wr = wrap.getBoundingClientRect();
            const sr = span.getBoundingClientRect();
            const POPOVER_W = 360;
            const POPOVER_H = 380;
            const left = Math.max(0, Math.min(sr.left - wr.left, wr.width - POPOVER_W));
            const spaceBelow = window.innerHeight - sr.bottom;
            // The flipped popover's visible top sits FLIP_GAP higher (CSS
            // translateY), so account for it when checking topbar clearance.
            const fitsAbove = (sr.top - POPOVER_H - FLIP_GAP) > TOPBAR_H;
            const above = spaceBelow < POPOVER_H + 12 && fitsAbove;
            const top = above ? (sr.top - wr.top) : (sr.bottom - wr.top + FLIP_GAP);
            return { top: top, left: left, above: above };
        },
        closePicker() {
            this.picker = emptyPicker();
        },
        // Narrow the stage-2 field list by the search box; stored so the list
        // isn't re-filtered on every unrelated reactive tick.
        applyFieldFilter() {
            const q = (this.picker.query || '').trim().toLowerCase();
            this.picker.filtered = q
                ? this.picker.paths.filter((p) => p.path.toLowerCase().includes(q))
                : this.picker.paths;
        },
        // Fields offered for a source step come from its *sent* response only —
        // until the step is sent there is nothing to parse (per the spec, the
        // list appears after the previous step actually runs).
        loadSourcePaths(stepNum) {
            const src = this.steps[stepNum - 1];
            if (src && src.response) {
                try { return this.leafPaths(JSON.parse(src.response.body)); } catch (e) { return []; }
            }
            return [];
        },
        selectPickerSource(stepNum) {
            this.picker.sourceStep = stepNum;
            this.picker.paths = this.loadSourcePaths(stepNum);
            this.picker.query = '';
            this.applyFieldFilter();
        },
        // Flatten a parsed JSON value to its leaf paths (a.b, a.c[0].d ...).
        leafPaths(obj, prefix) {
            prefix = prefix || '';
            if (obj === null || typeof obj !== 'object') {
                return prefix ? [{ path: prefix }] : [];
            }
            let out = [];
            if (Array.isArray(obj)) {
                if (obj.length === 0) return prefix ? [{ path: prefix }] : [];
                obj.forEach((v, i) => { out = out.concat(this.leafPaths(v, prefix + '[' + i + ']')); });
            } else {
                const keys = Object.keys(obj);
                if (keys.length === 0) return prefix ? [{ path: prefix }] : [];
                keys.forEach((k) => {
                    const p = prefix ? prefix + '.' + k : k;
                    out = out.concat(this.leafPaths(obj[k], p));
                });
            }
            return out;
        },
        async bindField(stepNum, path) {
            const idx = this.picker.stepIdx;
            if (idx === null) return;
            const step = this.steps[idx];
            const location = this.picker.location;
            try {
                const r = await fetch(base + this.chainId + '/steps/' + step.id + '/bind', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({ location: location, ref_step: stepNum, ref_path: path }),
                });
                if (!r.ok) throw new Error(await this.errorMessage(r, 'Не удалось привязать поле'));
                const d = await r.json();
                step.body = d.body;
                step.bodyHtml = d.body_html;
                this.invalidate(idx);
                this.closePicker();
                this.toast('Поле привязано к ответу шага ' + stepNum);
            } catch (e) {
                this.toast(e.message || 'Не удалось привязать поле', 'error');
            }
        },
        async unbindField() {
            const idx = this.picker.stepIdx;
            if (idx === null) return;
            const step = this.steps[idx];
            const location = this.picker.location;
            try {
                const r = await fetch(base + this.chainId + '/steps/' + step.id + '/unbind', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({ location: location }),
                });
                if (!r.ok) throw new Error(await this.errorMessage(r, 'Не удалось сбросить привязку'));
                const d = await r.json();
                step.body = d.body;
                step.bodyHtml = d.body_html;
                this.invalidate(idx);
                this.closePicker();
                this.toast('Привязка сброшена');
            } catch (e) {
                this.toast(e.message || 'Не удалось сбросить привязку', 'error');
            }
        },
        // Pull the server's {message: …} off a failed response, falling back to
        // a generic label so the user sees the concrete reason (e.g. «Поле не
        // найдено в теле запроса») instead of an opaque HTTP code.
        async errorMessage(r, fallback) {
            try {
                const d = await r.json();
                return (d && d.message) || fallback;
            } catch (e) { return fallback; }
        },

        // ---------- resolution (for the stub send) ----------
        escapeHtml(s) {
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        },
        getPath(obj, path) {
            const parts = path.replace(/\[(\d+)\]/g, '.$1').split('.').filter(Boolean);
            let cur = obj;
            for (const p of parts) {
                if (cur == null) return undefined;
                cur = cur[p];
            }
            return cur;
        },
        // The JSON a reference reads from: the actual response once the step has
        // been sent, otherwise its (hidden) example response.
        sourceBody(step) {
            if (!step) return '';
            return step.response ? step.response.body : (step.mockResponse || '');
        },
        resolveValue(stepNum, path) {
            const src = this.steps[stepNum - 1];
            if (!src) return undefined;
            let obj;
            try { obj = JSON.parse(this.sourceBody(src)); } catch (e) { return undefined; }
            return this.getPath(obj, path);
        },
        // Advance a "currently inside a JSON string literal" flag across a chunk
        // of template text, honouring backslash escapes.
        scanJsonStringState(inString, chunk) {
            let i = 0;
            while (i < chunk.length) {
                const ch = chunk[i];
                if (inString) {
                    if (ch === '\\') { i += 2; continue; }
                    if (ch === '"') inString = false;
                } else if (ch === '"') {
                    inString = true;
                }
                i++;
            }
            return inString;
        },
        // Encode a resolved value so the substituted result stays valid:
        //  - JSON value position  -> JSON.stringify (string -> "...", number -> 123)
        //  - inside a JSON string -> escaped chars WITHOUT wrapping quotes
        //  - non-JSON body        -> raw string
        encodeRef(val, isJson, inString) {
            if (!isJson) {
                return (typeof val === 'string') ? val : JSON.stringify(val);
            }
            if (inString) {
                const s = (typeof val === 'string') ? val : JSON.stringify(val);
                return JSON.stringify(s).slice(1, -1);
            }
            return JSON.stringify(val);
        },
        resolveBody(idx) {
            const step = this.steps[idx];
            const src = step.body || '';
            const isJson = (step.format || 'json') === 'json';
            const re = /\{\{\s*\$(\d+)\.([^}\s]+)\s*\}\}/g;
            const unresolved = [];
            let refs = 0;
            let html = '';
            let resolved = '';
            let last = 0;
            let inString = false;
            let m;
            while ((m = re.exec(src)) !== null) {
                refs++;
                const before = src.slice(last, m.index);
                html += this.escapeHtml(before);
                resolved += before;
                if (isJson) inString = this.scanJsonStringState(inString, before);
                const val = this.resolveValue(parseInt(m[1], 10), m[2]);
                if (val === undefined) {
                    unresolved.push(m[0]);
                    html += '<span class="placeholder reference unresolved" title="Не удалось разрешить ссылку">'
                        + this.escapeHtml(m[0]) + '</span>';
                    resolved += m[0];
                } else {
                    const strVal = this.encodeRef(val, isJson, inString);
                    html += '<span class="placeholder reference" title="Из ответа шага ' + m[1] + '">'
                        + this.escapeHtml(strVal) + '</span>';
                    resolved += strVal;
                }
                last = re.lastIndex;
            }
            const tail = src.slice(last);
            html += this.escapeHtml(tail);
            resolved += tail;
            return { resolved: resolved, html: html, unresolved: unresolved, refs: refs };
        },

        // ---------- "send" (mock) ----------
        async send(idx) {
            const step = this.steps[idx];
            step.error = '';
            step.sending = true;
            const res = this.resolveBody(idx);
            step.resolvedRequestHtml = res.html;
            step.resolvedRefs = res.refs > 0;
            step.resolvedUnresolved = res.unresolved.length > 0;
            if (res.unresolved.length) {
                step.error = 'Не разрешены ссылки: ' + res.unresolved.join(', ');
                step.response = null;
                step.sending = false;
                return;
            }
            try {
                const r = await fetch(this.executeUrl, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        method: step.method,
                        url: step.url,
                        headers: step.headers,
                        body: res.resolved,
                        format: step.format,
                        mock_response: step.mockResponse,
                    }),
                });
                if (!r.ok) throw new Error('HTTP ' + r.status);
                const d = await r.json();
                step.response = {
                    status: d.status,
                    statusText: d.status_text,
                    latencyMs: d.latency_ms,
                    body: this.prettyJson(d.body),
                };
            } catch (e) {
                step.error = 'Ошибка отправки (мок)';
            } finally {
                step.sending = false;
            }
        },
        async sendAll() {
            this.running = true;
            try {
                for (let i = 0; i < this.steps.length; i++) {
                    await this.send(i);
                    // Stop on the first failing step — later steps may reference
                    // its (now absent) response.
                    if (this.steps[i].error) break;
                }
            } finally {
                this.running = false;
            }
        },
        prettyJson(s) {
            try { return JSON.stringify(JSON.parse(s), null, 2); } catch (e) { return s; }
        },
        // Binding a field changes this step's body, so its own last run no longer
        // matches. Later steps may reference this step's response, so their
        // resolved-request preview is now stale too — clear it (keep their
        // responses, which the field picker still reads from).
        invalidate(idx) {
            const step = this.steps[idx];
            step.response = null;
            step.resolvedRequestHtml = null;
            step.resolvedRefs = false;
            step.resolvedUnresolved = false;
            step.error = '';
            for (let j = idx + 1; j < this.steps.length; j++) {
                this.steps[j].resolvedRequestHtml = null;
                this.steps[j].resolvedRefs = false;
                this.steps[j].resolvedUnresolved = false;
            }
        },

        toast(message, type) {
            window.dispatchEvent(new CustomEvent('show-toast', {
                detail: { message: message, type: type || 'success' },
            }));
        },
    };
};
