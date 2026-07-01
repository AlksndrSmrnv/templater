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

    // ---- dynamic field generation (rqUID / operUID / rqTm / channelDateTime) ----
    // A per-field pattern string (configured in settings) is expanded into a
    // concrete value at send time and substituted into the request body/headers.
    // Grammar (mirrors the settings help): {uuid} {uuid_upper} {rand:N} {hex:N}
    // {seq} {timestamp} {timestamp_ms} {date:FORMAT}. Unknown {...} tokens are
    // left verbatim so a typo is visible rather than silently dropped.
    let dynamicSeq = 0;

    function uuidv4() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
            const r = (Math.random() * 16) | 0;
            const v = c === 'x' ? r : (r & 0x3) | 0x8;
            return v.toString(16);
        });
    }
    function randDigits(n) {
        let out = '';
        for (let i = 0; i < n; i++) out += Math.floor(Math.random() * 10);
        return out;
    }
    function randHex(n) {
        let out = '';
        for (let i = 0; i < n; i++) out += Math.floor(Math.random() * 16).toString(16);
        return out;
    }
    // Signed timezone offset for the given date, as ±HH:MM (colon) / ±HHMM.
    function tzOffset(d, colon) {
        const mins = -d.getTimezoneOffset();
        const sign = mins >= 0 ? '+' : '-';
        const abs = Math.abs(mins);
        const hh = pad(Math.floor(abs / 60));
        const mm = pad(abs % 60);
        return sign + hh + (colon ? ':' : '') + mm;
    }
    // moment-like date formatting in the browser's local zone. Longest tokens
    // first so YYYY isn't eaten by YY, SSS before ss, ZZ before Z.
    function formatDate(fmt, d) {
        const map = {
            YYYY: String(d.getFullYear()),
            YY: pad(d.getFullYear() % 100),
            MM: pad(d.getMonth() + 1),
            DD: pad(d.getDate()),
            HH: pad(d.getHours()),
            mm: pad(d.getMinutes()),
            ss: pad(d.getSeconds()),
            SSS: String(d.getMilliseconds()).padStart(3, '0'),
            ZZ: tzOffset(d, true),
            Z: tzOffset(d, false),
        };
        return fmt.replace(/YYYY|YY|MM|DD|HH|mm|ss|SSS|ZZ|Z/g, function (t) { return map[t]; });
    }
    // A positive integer argument, or null when absent/malformed. Used so
    // {rand}/{hex} without a valid count leave the token verbatim (a visible
    // mistake) rather than silently expanding to an empty string.
    function positiveArg(arg) {
        if (arg === undefined || !/^\d+$/.test(arg)) return null;
        const n = parseInt(arg, 10);
        return n > 0 ? n : null;
    }
    // Expand one pattern. `now` is passed in so several fields generated for the
    // same message share a single timestamp. Unknown tokens, and count/format
    // tokens with a missing or invalid argument, are left verbatim.
    window.generateDynamicValue = function (pattern, now) {
        const d = now || new Date();
        const src = (typeof pattern === 'string' && pattern) ? pattern : '';
        return src.replace(/\{(uuid|uuid_upper|rand|hex|seq|timestamp|timestamp_ms|date)(?::([^}]*))?\}/g,
            function (whole, name, arg) {
                switch (name) {
                    case 'uuid': return uuidv4();
                    case 'uuid_upper': return uuidv4().toUpperCase();
                    case 'rand': { const n = positiveArg(arg); return n === null ? whole : randDigits(n); }
                    case 'hex': { const n = positiveArg(arg); return n === null ? whole : randHex(n); }
                    case 'seq': return String(++dynamicSeq);
                    case 'timestamp': return String(Math.floor(d.getTime() / 1000));
                    case 'timestamp_ms': return String(d.getTime());
                    case 'date': return arg ? formatDate(arg, d) : whole;
                    default: return whole;
                }
            });
    };

    // ---- shared dynamic-field context / substitution (chain + filled sends) ----
    // Canonical envelope tokens whose values are (re)generated per send. Kept in
    // one place so both send panels agree on names, operUID scoping, and the
    // header/body substitution rules.
    const DYNAMIC_FIELD_DEFAULTS = {
        rqUID: '{uuid}',
        operUID: '{uuid}',
        rqTm: '{date:YYYY-MM-DDTHH:mm:ss}',
        channelDateTime: '{date:YYYY-MM-DDTHH:mm:ss}',
    };
    // Case-insensitive lookup of a bareword {{token}} against a context object.
    // Returns the generated string, or undefined for a non-dynamic word.
    function dynamicLookup(ctx, word) {
        if (!ctx) return undefined;
        const target = String(word).toLowerCase();
        for (const key of Object.keys(ctx)) {
            if (key.toLowerCase() === target) return ctx[key];
        }
        return undefined;
    }
    window.dynamicFields = {
        DEFAULTS: DYNAMIC_FIELD_DEFAULTS,
        generate: window.generateDynamicValue,
        lookup: dynamicLookup,
        // Generate the four dynamic values for one message. `sharedOperuid`, when
        // non-empty, pins operUID to a value minted once for a whole real-chain
        // run; everything else is fresh. All fields share one `now` so the two
        // datetime fields line up to the same instant.
        buildContext(patterns, sharedOperuid, now) {
            const p = patterns || {};
            const d = now || new Date();
            const gen = (key) => window.generateDynamicValue(p[key] || DYNAMIC_FIELD_DEFAULTS[key], d);
            return {
                rqUID: gen('rqUID'),
                operUID: (sharedOperuid != null && sharedOperuid !== '') ? sharedOperuid : gen('operUID'),
                rqTm: gen('rqTm'),
                channelDateTime: gen('channelDateTime'),
            };
        },
        // Substitute dynamic tokens into a plain-text string (e.g. a header
        // value); unknown / reference tokens are left untouched.
        substitute(text, ctx) {
            return String(text == null ? '' : text).replace(
                /\{\{\s*([A-Za-z][A-Za-z0-9]*)\s*\}\}/g,
                (whole, word) => {
                    const v = dynamicLookup(ctx, word);
                    return v === undefined ? whole : String(v);
                });
        },
        // Enabled headers ({key,value}) with dynamic tokens substituted.
        resolveHeaders(headers, ctx) {
            return (headers || [])
                .filter((h) => h && !h.disabled)
                .map((h) => ({ key: h.key, value: this.substitute(h.value, ctx) }));
        },
    };

    // Node-only export so the pure logic above can be unit-tested without a
    // browser/DOM (see tests/js/). No-op in the browser.
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            generateDynamicValue: window.generateDynamicValue,
            dynamicFields: window.dynamicFields,
        };
    }
})();
