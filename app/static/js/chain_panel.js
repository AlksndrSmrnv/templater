/*
 * Alpine component for the «Цепочка запросов» panel (inline in the «Заполненные
 * шаблоны» workspace and on the standalone chain page).
 *
 * Steps are seeded from the server (DB-persisted). Per-step edits (body, example
 * response), reordering and removal autosave via fetch; structural *adds* are
 * HTMX-driven (the whole panel re-renders from the server). Sending is a STUB —
 * no real network request is made: the server seam echoes the step's editable
 * example response back so later steps can pull fields from it via
 * `{{ $N.path }}` tokens, highlighted purple.
 *
 * The resolution/highlight/encoding helpers are ported from the (reverted) PR
 * #92 send page — they were the correct part of that work.
 *
 * Defined on window so HTMX-swapped panels can reference it without a per-swap
 * <script> race; loaded once at page level.
 */
window.chainPanel = function (config) {
    const base = '/templater/filled-templates-htmx/chains/';
    return {
        chainId: config.chainId,
        executeUrl: config.executeUrl,
        steps: [],
        pickerOpen: false,
        pickerSearch: '',
        running: false,
        refUI: { stepIdx: null, sourceStep: null, paths: [] },

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
                mockResponse: s.mock_response || '',
                editingBody: false,
                sending: false,
                error: '',
                response: null,
                resolvedRequestHtml: null,
            }));
        },

        // ---------- picker (client-side filter of server-rendered options) ----------
        matchPicker(name) {
            const q = this.pickerSearch.trim().toLowerCase();
            return !q || String(name || '').toLowerCase().includes(q);
        },

        // ---------- persistence ----------
        async saveStep(idx) {
            const step = this.steps[idx];
            if (!step) return;
            try {
                await fetch(base + this.chainId + '/steps/' + step.id, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({ body: step.body, mock_response: step.mockResponse }),
                });
            } catch (e) { /* autosave is best-effort; next edit retries */ }
        },
        async persistOrder() {
            const order = this.steps.map((s) => s.id).join(',');
            try {
                await fetch(base + this.chainId + '/steps/reorder', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({ order: order }),
                });
            } catch (e) { /* ignore */ }
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
            this.steps.splice(idx, 1);
            this.closeRefUI();
            try {
                await fetch(base + this.chainId + '/steps/' + step.id, { method: 'DELETE' });
            } catch (e) { /* ignore */ }
            this.refreshTree();
            this.toast('Шаг удалён');
        },
        async moveStep(idx, delta) {
            const j = idx + delta;
            if (j < 0 || j >= this.steps.length) return;
            const [s] = this.steps.splice(idx, 1);
            this.steps.splice(j, 0, s);
            // Indices shift on reorder, so a reference panel would point at the
            // wrong step — close it.
            this.closeRefUI();
            await this.persistOrder();
        },

        // ---------- references / dependencies / highlighting ----------
        escapeHtml(s) {
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
        // Wrap {{ $N.path }} tokens in a purple span inside an escaped body. The
        // pattern matches resolveBody's exactly — a token without a path is
        // neither highlighted nor resolved.
        highlightRefs(text) {
            return this.escapeHtml(text).replace(/\{\{\s*\$\d+\.[^}\s]+\s*\}\}/g, (m) =>
                '<span class="placeholder reference" title="Ссылка на ответ предыдущего шага">' + m + '</span>');
        },
        // Prior step numbers (1-based) a step references — drives «зависит от шага N».
        stepDeps(idx) {
            const body = (this.steps[idx] && this.steps[idx].body) || '';
            const re = /\{\{\s*\$(\d+)\.[^}\s]+\s*\}\}/g;
            const out = [];
            let m;
            while ((m = re.exec(body)) !== null) {
                const n = parseInt(m[1], 10);
                if (!out.includes(n)) out.push(n);
            }
            return out.sort((a, b) => a - b);
        },
        priorSteps(idx) {
            return this.steps.slice(0, idx).map((s, i) => ({
                num: i + 1, name: s.name, sent: !!s.response,
            }));
        },
        openRefUI(idx) {
            if (idx === 0) return;
            this.refUI = { stepIdx: idx, sourceStep: null, paths: [] };
        },
        closeRefUI() {
            this.refUI = { stepIdx: null, sourceStep: null, paths: [] };
        },
        // The JSON a reference reads from: the actual response once the step has
        // been sent, otherwise its (editable) example response.
        sourceBody(step) {
            if (!step) return '';
            return step.response ? step.response.body : (step.mockResponse || '');
        },
        selectRefSource(stepNum) {
            const src = this.steps[stepNum - 1];
            let paths = [];
            if (src) {
                try { paths = this.leafPaths(JSON.parse(this.sourceBody(src))); } catch (e) { paths = []; }
            }
            this.refUI.sourceStep = stepNum;
            this.refUI.paths = paths;
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
        insertRef(stepNum, path) {
            const idx = this.refUI.stepIdx;
            if (idx === null) return;
            const step = this.steps[idx];
            const token = '{{ $' + stepNum + '.' + path + ' }}';
            const ta = document.getElementById('body-ta-' + step.id);
            if (step.editingBody && ta && typeof ta.selectionStart === 'number') {
                const start = ta.selectionStart;
                const end = ta.selectionEnd;
                const b = step.body || '';
                step.body = b.slice(0, start) + token + b.slice(end);
                this.$nextTick(() => {
                    ta.focus();
                    const pos = start + token.length;
                    ta.setSelectionRange(pos, pos);
                });
            } else {
                // Not editing: switch to edit mode and append so the user can
                // place the token where it belongs.
                step.editingBody = true;
                step.body = (step.body || '') + token;
            }
            this.closeRefUI();
            this.invalidate(idx);
            this.toast('Ссылка вставлена');
        },

        // ---------- resolution ----------
        getPath(obj, path) {
            const parts = path.replace(/\[(\d+)\]/g, '.$1').split('.').filter(Boolean);
            let cur = obj;
            for (const p of parts) {
                if (cur == null) return undefined;
                cur = cur[p];
            }
            return cur;
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
            let html = '';
            let resolved = '';
            let last = 0;
            let inString = false;
            let m;
            while ((m = re.exec(src)) !== null) {
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
            return { resolved: resolved, html: html, unresolved: unresolved };
        },

        // ---------- "send" (mock) ----------
        async send(idx) {
            const step = this.steps[idx];
            step.error = '';
            step.sending = true;
            const res = this.resolveBody(idx);
            step.resolvedRequestHtml = res.html;
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
        // Editing the body or the example response invalidates the last run.
        invalidate(idx) {
            const step = this.steps[idx];
            step.response = null;
            step.resolvedRequestHtml = null;
            step.error = '';
            this.saveStep(idx);
        },

        toast(message, type) {
            window.dispatchEvent(new CustomEvent('show-toast', {
                detail: { message: message, type: type || 'success' },
            }));
        },
    };
};
