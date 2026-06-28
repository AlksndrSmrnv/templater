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
})();
