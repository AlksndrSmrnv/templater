/*
 * Shared statusCode extraction for the «send»/«цепочка запросов» panels.
 *
 * Both chain_panel.js and the inline Alpine data in filled_panel.html surface a
 * statusCode indicator next to their send buttons. The lookup lives here so the
 * rules stay in one place. Loaded (defer) before chain_panel.js and on every
 * page that renders the filled-template panel, exposed as window.extractStatusCode.
 */
(function () {
    // Depth-first search for the first non-null result `pick(obj)` yields, with
    // the SAME order both lookups below rely on: an object's own keys are tried
    // (via pick) before descending into its values; arrays are walked in order.
    function findFirst(node, pick) {
        if (node === null || typeof node !== 'object') return null;
        if (Array.isArray(node)) {
            for (const v of node) {
                const r = findFirst(v, pick);
                if (r !== null) return r;
            }
            return null;
        }
        const hit = pick(node);
        if (hit !== null) return hit;
        for (const k of Object.keys(node)) {
            const r = findFirst(node[k], pick);
            if (r !== null) return r;
        }
        return null;
    }

    // Picker for `statusCode` (case-insensitive). Only real numbers and numeric
    // strings count — booleans are NOT coerced (true→1 / false→0 would mislead).
    function pickStatusCode(node) {
        for (const k of Object.keys(node)) {
            if (k.toLowerCase() === 'statuscode') {
                const v = node[k];
                if (typeof v === 'number' && !Number.isNaN(v)) return v;
                if (typeof v === 'string' && /^-?\d+(\.\d+)?$/.test(v.trim())) return Number(v);
            }
        }
        return null;
    }

    // Picker for an arbitrary scalar field named `target` (already lower-cased).
    function pickField(target) {
        return function (node) {
            for (const k of Object.keys(node)) {
                if (k.toLowerCase() === target) {
                    const v = node[k];
                    if (typeof v === 'string' || typeof v === 'number') return String(v);
                }
            }
            return null;
        };
    }

    // Accept either a JSON string or an already-parsed value, so a caller that
    // needs several fields can JSON.parse once and pass the object to each call.
    function asNode(input) {
        if (input !== null && typeof input === 'object') return input;
        try { return JSON.parse(input); } catch (e) { return null; }
    }

    // Pull the statusCode out of a response body. null on parse error or when the
    // field is absent / non-numeric. A statusCode of 0 is returned as 0 (≠ null).
    window.extractStatusCode = function (input) {
        return findFirst(asNode(input), pickStatusCode);
    };

    // Recursive lookup of the first scalar field named `name` (any case) at any
    // nesting depth — used to surface business identifiers (operuid, suit) buried
    // anywhere in a response body. Returns the value as a string, or null.
    window.extractField = function (input, name) {
        return findFirst(asNode(input), pickField(String(name).toLowerCase()));
    };

    // ---- «last send» timestamp formatting (shared by both send panels) ----
    // Server seeds badges with ISO-8601 (UTC) timestamps; both the seeded value
    // and a locally produced «now» are rendered through this single formatter, so
    // they always read in the SAME (browser-local) zone — no UTC-vs-local drift
    // between a server-rendered badge and one updated right after a send.
    const pad = (n) => String(n).padStart(2, '0');
    // Format an ISO-8601 string as dd.mm.yyyy HH:MM:SS in the browser's zone.
    window.formatSendTs = function (iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return iso;
        return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear()
            + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    };
    // ISO-8601 (UTC) for «now» — store this on a badge after a local send, then
    // render it through formatSendTs like any server-seeded value.
    window.nowSendTs = function () { return new Date().toISOString(); };
})();
