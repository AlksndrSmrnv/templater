/*
 * Unit tests for the browser-side send seam (app/static/js/rest_sender.js) —
 * mock parity with the retired server stub, header flattening, and the real
 * branch's response/error mapping with an injected fetch.
 *
 * Run: `node --test tests/js/`  (Node 18+; no dependencies).
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');

// Browser IIFEs that attach to `window`; shim it before requiring. The seam
// reads window.extractStatusCode at call time, so load status_code.js first.
global.window = {};
require('../../app/static/js/status_code.js');
const { restSender, headerPairs, computeOk, errorText } = require('../../app/static/js/rest_sender.js');

// Deterministic deps for the mock branch: zero sleep, fixed random.
function mockDeps(randomValue) {
    const slept = [];
    return {
        deps: {
            random: () => randomValue,
            sleep: (ms) => { slept.push(ms); return Promise.resolve(); },
        },
        slept,
    };
}

// A fetch double returning a Response-like object; records the call.
function fakeFetch(response) {
    const calls = [];
    const impl = (url, opts) => {
        calls.push({ url, opts });
        return Promise.resolve(response);
    };
    return { impl, calls };
}

function fakeResponse({ status = 200, statusText = 'OK', body = '', headers = {}, type = 'basic' } = {}) {
    return {
        type,
        status,
        statusText,
        headers: { forEach: (cb) => Object.entries(headers).forEach(([k, v]) => cb(v, k)) },
        text: () => Promise.resolve(body),
    };
}

// ---- mock branch -----------------------------------------------------------

test('mock: echoes mockResponse with the stub status/headers', async () => {
    const { deps } = mockDeps(0);
    const d = await restSender.send({ real: false, mockResponse: '{"statusCode": 0}' }, deps);
    assert.strictEqual(d.ok, true);
    assert.strictEqual(d.status, 200);
    assert.strictEqual(d.status_text, 'OK');
    assert.strictEqual(d.body, '{"statusCode": 0}');
    assert.strictEqual(d.error, '');
    assert.deepStrictEqual(d.headers, {
        'Content-Type': 'application/json; charset=utf-8',
        'X-Mock-Send': 'true',
    });
});

test('mock: ok semantics — absent/0 ok, non-zero statusCode not ok', async () => {
    const { deps } = mockDeps(0);
    assert.strictEqual((await restSender.send({ mockResponse: '{"a": 1}' }, deps)).ok, true);
    assert.strictEqual((await restSender.send({ mockResponse: '{"statusCode": 0}' }, deps)).ok, true);
    assert.strictEqual((await restSender.send({ mockResponse: '{"statusCode": 5}' }, deps)).ok, false);
});

test('mock: simulated latency stays within the stub bounds (35..220)', async () => {
    const low = mockDeps(0);
    const dLow = await restSender.send({ mockResponse: '{}' }, low.deps);
    assert.strictEqual(dLow.latency_ms, 35);
    assert.deepStrictEqual(low.slept, [35]);
    const high = mockDeps(0.999999);
    const dHigh = await restSender.send({ mockResponse: '{}' }, high.deps);
    assert.strictEqual(dHigh.latency_ms, 220);
});

// ---- pure helpers -----------------------------------------------------------

test('headerPairs: skips disabled and blank keys, preserves falsy "0" value', () => {
    assert.deepStrictEqual(
        headerPairs([
            { key: 'A', value: '1' },
            { key: 'B', value: '2', disabled: true },
            { key: '   ', value: 'x' },
            { key: 'C', value: '0' },
            { key: 'D' },
            null,
        ]),
        [['A', '1'], ['C', '0'], ['D', '']]
    );
    assert.deepStrictEqual(headerPairs(undefined), []);
});

test('computeOk: 2xx + statusCode absent/0 only', () => {
    assert.strictEqual(computeOk(200, null), true);
    assert.strictEqual(computeOk(204, 0), true);
    assert.strictEqual(computeOk(200, 7), false);
    assert.strictEqual(computeOk(500, null), false);
    assert.strictEqual(computeOk(302, null), false);
    assert.strictEqual(computeOk(null, null), false);
});

test('errorText: timeout, mixed content, and the generic CORS/network hint', () => {
    assert.match(errorText({ name: 'AbortError' }, 'https://x', 'https:'), /таймаут/);
    assert.match(errorText(new TypeError('failed'), 'http://x', 'https:'), /mixed content/);
    const generic = errorText(new TypeError('failed'), 'https://x', 'https:');
    assert.match(generic, /CORS/);
    assert.match(generic, /клиентский сертификат/);
});

// ---- real branch ------------------------------------------------------------

test('real: 200 + statusCode 0 is ok; response mapped verbatim', async () => {
    const { impl, calls } = fakeFetch(fakeResponse({
        status: 200, statusText: 'OK',
        body: '{"statusCode": 0, "operuid": "OP-1"}',
        headers: { 'content-type': 'application/json' },
    }));
    const d = await restSender.send({
        real: true, method: 'post', url: 'https://svc/pay', format: 'json',
        headers: [{ key: 'RqUID', value: '1' }], body: '{"a": 1}',
    }, { fetchImpl: impl });
    assert.strictEqual(d.ok, true);
    assert.strictEqual(d.status, 200);
    assert.strictEqual(d.body, '{"statusCode": 0, "operuid": "OP-1"}');
    assert.deepStrictEqual(d.headers, { 'content-type': 'application/json' });
    assert.strictEqual(typeof d.latency_ms, 'number');
    // Request assembly: method upper-cased, headers flattened, body passed.
    assert.strictEqual(calls[0].url, 'https://svc/pay');
    assert.strictEqual(calls[0].opts.method, 'POST');
    assert.strictEqual(calls[0].opts.body, '{"a": 1}');
    assert.strictEqual(calls[0].opts.redirect, 'manual');
    assert.strictEqual(calls[0].opts.headers.RqUID, '1');
});

test('real: 200 with non-zero business statusCode is not ok', async () => {
    const { impl } = fakeFetch(fakeResponse({ body: '{"statusCode": 7}' }));
    const d = await restSender.send({ real: true, url: 'https://x' }, { fetchImpl: impl });
    assert.strictEqual(d.ok, false);
    assert.strictEqual(d.status, 200);
});

test('real: 5xx is not ok but carries the response', async () => {
    const { impl } = fakeFetch(fakeResponse({ status: 500, statusText: 'Internal Server Error', body: 'boom' }));
    const d = await restSender.send({ real: true, url: 'https://x' }, { fetchImpl: impl });
    assert.strictEqual(d.ok, false);
    assert.strictEqual(d.status, 500);
    assert.strictEqual(d.status_text, 'Internal Server Error');
    assert.strictEqual(d.body, 'boom');
});

test('real: opaqueredirect maps to a not-ok result with the 3xx hint', async () => {
    const { impl } = fakeFetch({ type: 'opaqueredirect', status: 0 });
    const d = await restSender.send({ real: true, url: 'https://x' }, { fetchImpl: impl });
    assert.strictEqual(d.ok, false);
    assert.strictEqual(d.status, null);
    assert.match(d.error, /перенаправлением/);
});

test('real: a rejected fetch (CORS/network) yields the hint list, no throw', async () => {
    const impl = () => Promise.reject(new TypeError('Failed to fetch'));
    const d = await restSender.send({ real: true, url: 'https://x' }, { fetchImpl: impl });
    assert.strictEqual(d.ok, false);
    assert.strictEqual(d.status, null);
    assert.match(d.error, /CORS/);
});

test('real: GET drops the body (fetch would throw)', async () => {
    const { impl, calls } = fakeFetch(fakeResponse({ body: '{}' }));
    await restSender.send(
        { real: true, method: 'GET', url: 'https://x', body: '{"a": 1}' },
        { fetchImpl: impl }
    );
    assert.strictEqual(calls[0].opts.body, undefined);
});

test('real: Content-Type defaults from format when absent, kept when given', async () => {
    const { impl, calls } = fakeFetch(fakeResponse({ body: '{}' }));
    const deps = { fetchImpl: impl };
    await restSender.send({ real: true, method: 'POST', url: 'https://x', body: '<a/>', format: 'xml' }, deps);
    assert.strictEqual(calls[0].opts.headers['Content-Type'], 'application/xml');
    await restSender.send({ real: true, method: 'POST', url: 'https://x', body: '{}', format: 'json' }, deps);
    assert.strictEqual(calls[1].opts.headers['Content-Type'], 'application/json');
    await restSender.send({
        real: true, method: 'POST', url: 'https://x', body: '{}', format: 'json',
        headers: [{ key: 'content-type', value: 'text/plain' }],
    }, deps);
    assert.strictEqual(calls[2].opts.headers['content-type'], 'text/plain');
    assert.strictEqual(calls[2].opts.headers['Content-Type'], undefined);
});
