/*
 * Browser-side REST sending seam — the ONE place that turns a prepared request
 * into a result. Mirrors the retired server seam (app/services/rest_sender.py):
 *
 *  - mock (real=false): no network call — echoes the caller's editable
 *    mock_response after a small simulated latency, exactly as the old stub.
 *  - real (real=true): a direct fetch() from THIS browser to the target URL.
 *    Сообщение не проходит через наш бэкенд; клиентский сертификат (mTLS)
 *    браузер берёт из системного хранилища ОС, серверный сертификат всегда
 *    проверяется браузером (отключить это из JS невозможно).
 *
 * Result shape is the same JSON the old /send-htmx/execute endpoint returned,
 * so panel code keeps reading d.ok / d.status / d.body / d.error unchanged:
 *   { ok, status, status_text, latency_ms, headers, body, error }
 *
 * Loaded (defer) after status_code.js (needs window.extractStatusCode).
 */
(function () {
    var REQUEST_TIMEOUT_MS = 30000;

    // The mock's response headers/status — kept identical to the old server stub
    // so history rows and UI badges are unchanged when real sending is off.
    var MOCK_RESPONSE_HEADERS = {
        'Content-Type': 'application/json; charset=utf-8',
        'X-Mock-Send': 'true',
    };

    // Flatten resolved header rows ({key,value,disabled?}) to [key, value]
    // pairs, skipping disabled/blank-key rows. A legitimate falsy value (e.g.
    // "0") must survive verbatim — mirror of the server's _header_pairs.
    function headerPairs(rows) {
        var out = [];
        (Array.isArray(rows) ? rows : []).forEach(function (h) {
            if (!h || typeof h !== 'object' || h.disabled) return;
            var key = String(h.key == null ? '' : h.key).trim();
            if (!key) return;
            out.push([key, h.value == null ? '' : String(h.value)]);
        });
        return out;
    }

    // Overall ok = transport succeeded (2xx) AND no non-zero business statusCode
    // in the body — the same semantics for mock and real.
    function computeOk(httpStatus, statusCode) {
        var transport = typeof httpStatus === 'number' && httpStatus >= 200 && httpStatus < 300;
        return transport && (statusCode === null || statusCode === 0);
    }

    // Human-readable (Russian) text for a failed fetch. The browser hides the
    // real cause of a network-layer failure (opaque TypeError), so the CORS/
    // network/cert possibilities are listed instead of guessed.
    function errorText(err, url, pageProtocol) {
        if (err && err.name === 'AbortError') {
            return 'Сервис не ответил за ' + (REQUEST_TIMEOUT_MS / 1000) + ' с — отправка прервана по таймауту.';
        }
        if (pageProtocol === 'https:' && /^http:\/\//i.test(String(url))) {
            return 'Браузер блокирует запрос на http:// со страницы https:// (mixed content). Используйте https-адрес сервиса.';
        }
        return 'Запрос не ушёл из браузера. Возможные причины: '
            + 'сервис не разрешает CORS-запросы с этого адреса; '
            + 'нет сетевого доступа до хоста; '
            + 'сервис требует клиентский сертификат, который не установлен в системе; '
            + 'самоподписанный сертификат сервера не является доверенным.';
    }

    function extractStatusCode(body) {
        return typeof window.extractStatusCode === 'function' ? window.extractStatusCode(body) : null;
    }

    function result(fields) {
        return {
            ok: !!fields.ok,
            status: fields.status != null ? fields.status : null,
            status_text: fields.status_text || '',
            latency_ms: fields.latency_ms != null ? fields.latency_ms : null,
            headers: fields.headers || {},
            body: fields.body || '',
            error: fields.error || '',
        };
    }

    // ---------- mock strategy ----------

    function sendMock(req, deps) {
        var latency = 35 + Math.floor(deps.random() * 186); // 35..220 ms, as the old stub
        return deps.sleep(latency).then(function () {
            var body = String(req.mockResponse == null ? '' : req.mockResponse);
            var statusCode = extractStatusCode(body);
            return result({
                ok: statusCode === null || statusCode === 0,
                status: 200,
                status_text: 'OK',
                latency_ms: latency,
                headers: Object.assign({}, MOCK_RESPONSE_HEADERS),
                body: body,
            });
        });
    }

    // ---------- real strategy (direct fetch from the browser) ----------

    function sendReal(req, deps) {
        var method = String(req.method || 'GET').toUpperCase();
        var pairs = headerPairs(req.headers);
        var headers = {};
        var hasContentType = false;
        pairs.forEach(function (kv) {
            if (kv[0].toLowerCase() === 'content-type') hasContentType = true;
            headers[kv[0]] = kv[1];
        });
        var body = String(req.body == null ? '' : req.body);
        // fetch() rejects a GET/HEAD with a body — drop it (httpx allowed it, but
        // the app is POST-centric; the envelope's method is what matters).
        var withBody = body !== '' && method !== 'GET' && method !== 'HEAD';
        // httpx sent no Content-Type at all; fetch would force text/plain, which
        // CORS-preflights differently and confuses JSON services — default it
        // from the template's format instead.
        if (withBody && !hasContentType) {
            headers['Content-Type'] = req.format === 'json' ? 'application/json' : 'application/xml';
        }
        var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
        var timer = controller ? setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS) : null;
        var started = deps.now();

        function elapsed() { return Math.round(deps.now() - started); }

        return deps.fetchImpl(String(req.url || ''), {
            method: method,
            headers: headers,
            body: withBody ? body : undefined,
            mode: 'cors',
            // Don't follow redirects — the old server seam treated a 3xx as an
            // unhandled (not-ok) response, not a success. The browser hides the
            // numeric 3xx code behind an opaqueredirect.
            redirect: 'manual',
            signal: controller ? controller.signal : undefined,
        }).then(function (r) {
            if (r.type === 'opaqueredirect') {
                return result({
                    ok: false,
                    latency_ms: elapsed(),
                    error: 'Сервис ответил перенаправлением (3xx); оно не выполняется, точный код скрыт браузером.',
                });
            }
            return r.text().then(function (text) {
                var respHeaders = {};
                r.headers.forEach(function (value, key) { respHeaders[key] = value; });
                var statusCode = extractStatusCode(text);
                return result({
                    ok: computeOk(r.status, statusCode),
                    status: r.status,
                    status_text: r.statusText || '',
                    latency_ms: elapsed(),
                    headers: respHeaders,
                    body: text,
                });
            });
        }).catch(function (err) {
            var pageProtocol = (typeof location !== 'undefined' && location.protocol) || '';
            return result({
                ok: false,
                latency_ms: elapsed(),
                error: errorText(err, req.url, pageProtocol),
            });
        }).finally(function () {
            if (timer) clearTimeout(timer);
        });
    }

    // ---------- public seam ----------

    window.restSender = {
        // send({method,url,headers,body,format,mockResponse,real}) -> Promise<result>
        // `deps` is test-only injection: {fetchImpl, sleep, random, now}.
        send: function (req, deps) {
            var d = deps || {};
            var full = {
                fetchImpl: d.fetchImpl || function (u, opts) { return fetch(u, opts); },
                sleep: d.sleep || function (ms) { return new Promise(function (res) { setTimeout(res, ms); }); },
                random: d.random || Math.random,
                now: d.now || function () {
                    return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
                },
            };
            return req && req.real ? sendReal(req, full) : sendMock(req, full);
        },
    };

    // Node-only export so the pure logic can be unit-tested without a browser
    // (see tests/js/rest_sender.test.js). No-op in the browser.
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            restSender: window.restSender,
            headerPairs: headerPairs,
            computeOk: computeOk,
            errorText: errorText,
        };
    }
})();
