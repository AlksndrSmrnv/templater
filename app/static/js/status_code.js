/*
 * Shared statusCode extraction for the «send»/«цепочка запросов» panels.
 *
 * Both chain_panel.js and the inline Alpine data in filled_panel.html surface a
 * statusCode indicator next to their send buttons. The lookup lives here so the
 * rules stay in one place. Loaded (defer) before chain_panel.js and on every
 * page that renders the filled-template panel, exposed as window.extractStatusCode.
 */
(function () {
    // Recursively find the first `statusCode` field (case-insensitive) at any
    // nesting depth, checking the current object's keys before descending.
    // Only real numbers and numeric strings count — booleans are NOT coerced
    // (true→1 / false→0 would be misleading). Returns the number or null.
    function findStatusCode(node) {
        if (node === null || typeof node !== 'object') return null;
        if (Array.isArray(node)) {
            for (const v of node) {
                const r = findStatusCode(v);
                if (r !== null) return r;
            }
            return null;
        }
        for (const k of Object.keys(node)) {
            if (k.toLowerCase() === 'statuscode') {
                const v = node[k];
                if (typeof v === 'number' && !Number.isNaN(v)) return v;
                if (typeof v === 'string' && /^-?\d+(\.\d+)?$/.test(v.trim())) return Number(v);
            }
        }
        for (const k of Object.keys(node)) {
            const r = findStatusCode(node[k]);
            if (r !== null) return r;
        }
        return null;
    }

    // Parse a JSON response body and pull out its statusCode (see findStatusCode).
    // null on parse error or when the field is absent / non-numeric.
    window.extractStatusCode = function (jsonStr) {
        let obj;
        try { obj = JSON.parse(jsonStr); } catch (e) { return null; }
        return findStatusCode(obj);
    };

    // Generic recursive lookup of the first scalar field named `name` (any case)
    // at any nesting depth — same traversal order as findStatusCode (the current
    // object's keys before descending). Used to surface business identifiers
    // (operuid, suit) buried anywhere in a response body. Returns the value as a
    // string, or null on parse error / when the field is absent or non-scalar.
    function findField(node, target) {
        if (node === null || typeof node !== 'object') return null;
        if (Array.isArray(node)) {
            for (const v of node) {
                const r = findField(v, target);
                if (r !== null) return r;
            }
            return null;
        }
        for (const k of Object.keys(node)) {
            if (k.toLowerCase() === target) {
                const v = node[k];
                if (typeof v === 'string' || typeof v === 'number') return String(v);
            }
        }
        for (const k of Object.keys(node)) {
            const r = findField(node[k], target);
            if (r !== null) return r;
        }
        return null;
    }

    window.extractField = function (jsonStr, name) {
        let obj;
        try { obj = JSON.parse(jsonStr); } catch (e) { return null; }
        return findField(obj, String(name).toLowerCase());
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
