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
    // Upper bound for {rand:N}/{hex:N} counts. Guards against a pathological
    // pattern (e.g. {rand:999999999}) freezing the tab in the generation loop —
    // an edit-mode user can only self-DoS, but the cap is cheap insurance.
    const MAX_COUNT = 64;
    // A positive integer argument within [1, MAX_COUNT], or null when
    // absent/malformed/out-of-range. Used so {rand}/{hex} without a valid count
    // leave the token verbatim (a visible mistake) rather than silently
    // expanding to an empty string (or hanging).
    function positiveArg(arg) {
        if (arg === undefined || !/^\d+$/.test(arg)) return null;
        const n = parseInt(arg, 10);
        return n >= 1 && n <= MAX_COUNT ? n : null;
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
    // ---- pure body-substitution helpers (shared with the chain body view) ----
    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    // Advance a "currently inside a JSON string literal" flag across a chunk of
    // template text, honouring backslash escapes.
    function scanJsonStringState(inString, chunk) {
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
    }
    // Encode a resolved value so the substituted result stays valid:
    //  - JSON value position  -> JSON.stringify (string -> "...", number -> 123)
    //  - inside a JSON string -> escaped chars WITHOUT wrapping quotes
    //  - non-JSON body        -> raw string
    function encodeRef(val, isJson, inString) {
        if (!isJson) {
            return (typeof val === 'string') ? val : JSON.stringify(val);
        }
        if (inString) {
            const s = (typeof val === 'string') ? val : JSON.stringify(val);
            return JSON.stringify(s).slice(1, -1);
        }
        return JSON.stringify(val);
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
        // Resolve a request body's tokens, producing both the sendable text and
        // coloured HTML for the chain view. Two token kinds share one scan:
        //   {{ $N.path }}  -> a reference to an earlier step's response (purple);
        //                     resolved via opts.resolveRef(stepNum, path) which
        //                     returns the value or undefined (left red/unresolved).
        //   {{ token }}    -> a dynamic envelope field (blue) looked up in opts.ctx,
        //                     or an untouched literal when unknown.
        // JSON bodies (opts.isJson) get proper encoding via encodeRef with
        // string-state tracking so a value landing inside a string vs a value
        // position is escaped correctly.
        resolveBody(src, opts) {
            opts = opts || {};
            const isJson = !!opts.isJson;
            const ctx = opts.ctx;
            const resolveRef = typeof opts.resolveRef === 'function'
                ? opts.resolveRef : function () { return undefined; };
            const text = String(src == null ? '' : src);
            // Match either a reference ($N.path) or a bareword token (dynamic).
            const re = /\{\{\s*(?:\$(\d+)\.([^}\s]+)|([A-Za-z][A-Za-z0-9]*))\s*\}\}/g;
            const unresolved = [];
            let refs = 0;
            let html = '';
            let resolved = '';
            let last = 0;
            let inString = false;
            let m;
            while ((m = re.exec(text)) !== null) {
                const before = text.slice(last, m.index);
                html += escapeHtml(before);
                resolved += before;
                if (isJson) inString = scanJsonStringState(inString, before);
                last = re.lastIndex;
                if (m[1] !== undefined) {
                    // ---- reference to an earlier step's response (purple) ----
                    refs++;
                    const val = resolveRef(parseInt(m[1], 10), m[2]);
                    if (val === undefined) {
                        unresolved.push(m[0]);
                        html += '<span class="placeholder reference unresolved" title="Не удалось разрешить ссылку">'
                            + escapeHtml(m[0]) + '</span>';
                        resolved += m[0];
                    } else {
                        const strVal = encodeRef(val, isJson, inString);
                        html += '<span class="placeholder reference" title="Из ответа шага ' + m[1] + '">'
                            + escapeHtml(strVal) + '</span>';
                        resolved += strVal;
                    }
                    continue;
                }
                // ---- bareword token: dynamic envelope field (blue) or literal ----
                const dynVal = dynamicLookup(ctx, m[3]);
                if (dynVal === undefined) {
                    // Not a dynamic token (or no context) — leave the text as-is.
                    html += escapeHtml(m[0]);
                    resolved += m[0];
                } else {
                    const strVal = encodeRef(String(dynVal), isJson, inString);
                    html += '<span class="placeholder dynamic" title="Динамическое поле ' + escapeHtml(m[3]) + '">'
                        + escapeHtml(strVal) + '</span>';
                    resolved += strVal;
                }
            }
            const tail = text.slice(last);
            html += escapeHtml(tail);
            resolved += tail;
            return { resolved: resolved, html: html, unresolved: unresolved, refs: refs };
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
