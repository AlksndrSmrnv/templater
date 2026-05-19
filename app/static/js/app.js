// Global utilities used across pages.

window.TM = window.TM || {};

TM.api = async function (method, url, body) {
    const opts = { method, headers: { "Accept": "application/json" } };
    if (body !== undefined) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    if (res.status === 204) return null;
    let data = null;
    try { data = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok) {
        const message = (data && (data.message || data.error)) || `HTTP ${res.status}`;
        const err = new Error(message);
        err.status = res.status;
        err.details = data && data.details;
        throw err;
    }
    return data;
};

TM.toast = function (message, type) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast" + (type ? " " + type : "");
    el.hidden = false;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.hidden = true; }, 4200);
};

TM.escapeHtml = function (str) {
    if (str === null || str === undefined) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
};

TM.formatDate = function (iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString("ru-RU");
};

TM.confirm = function (msg) { return window.confirm(msg); };
