// Workspace UI: перетаскиваемый разделитель между деревом и панелью + мгновенные
// тултипы с полным именем для обрезанных узлов дерева. Грузится на всех страницах,
// но активируется только при наличии .workspace (иначе no-op). Desktop-only.
(function () {
    'use strict';

    var STORAGE_KEY = 'tm:treeWidth';
    var DEFAULT_WIDTH = 320;
    var MIN_WIDTH = 240;

    function maxWidth() {
        return Math.min(Math.round(window.innerWidth * 0.6), 760);
    }

    function clampWidth(px) {
        return Math.max(MIN_WIDTH, Math.min(px, maxWidth()));
    }

    function applyWidth(workspace, px) {
        workspace.style.setProperty('--tree-w', px + 'px');
    }

    // ---- Resizer ----------------------------------------------------------
    function initResizer() {
        var workspace = document.querySelector('.workspace');
        if (!workspace) return;
        var left = workspace.querySelector('.workspace-left');
        if (!left || workspace.querySelector('.workspace-resizer')) return;

        // Применяем сохранённую ширину (инлайн-скрипт в <head> уже выставил
        // переменную на :root; дублируем на сам элемент для единообразия).
        var saved = null;
        try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) {}
        if (saved) applyWidth(workspace, clampWidth(parseInt(saved, 10) || DEFAULT_WIDTH));

        var resizer = document.createElement('div');
        resizer.className = 'workspace-resizer';
        resizer.setAttribute('role', 'separator');
        resizer.setAttribute('aria-orientation', 'vertical');
        resizer.title = 'Перетащите, чтобы изменить ширину • двойной клик — сброс';
        left.insertAdjacentElement('afterend', resizer);

        var startX = 0;
        var startW = 0;

        function onMove(e) {
            applyWidth(workspace, clampWidth(startW + (e.clientX - startX)));
        }

        function onUp(e) {
            resizer.classList.remove('dragging');
            document.body.classList.remove('workspace-resizing');
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);
            try { resizer.releasePointerCapture(e.pointerId); } catch (err) {}
            var current = parseInt(getComputedStyle(workspace).getPropertyValue('--tree-w'), 10);
            if (current) {
                try { localStorage.setItem(STORAGE_KEY, String(current)); } catch (err) {}
            }
        }

        resizer.addEventListener('pointerdown', function (e) {
            e.preventDefault();
            startX = e.clientX;
            var cur = parseInt(getComputedStyle(workspace).getPropertyValue('--tree-w'), 10);
            startW = cur || left.getBoundingClientRect().width || DEFAULT_WIDTH;
            resizer.classList.add('dragging');
            document.body.classList.add('workspace-resizing');
            try { resizer.setPointerCapture(e.pointerId); } catch (err) {}
            window.addEventListener('pointermove', onMove);
            window.addEventListener('pointerup', onUp);
        });

        // Двойной клик — сброс к ширине по умолчанию.
        resizer.addEventListener('dblclick', function () {
            applyWidth(workspace, DEFAULT_WIDTH);
            try { localStorage.removeItem(STORAGE_KEY); } catch (err) {}
        });
    }

    // ---- Мгновенные тултипы -----------------------------------------------
    var SELECTOR = '.tree-name, .tree-folder-name, .tree-collection-name';
    var tip = null;
    var activeEl = null;

    function isTruncated(el) {
        return el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1;
    }

    function hideTip() {
        if (tip) { tip.remove(); tip = null; }
        if (activeEl) {
            // Возвращаем нативный title, временно снятый ради подавления
            // медленного браузерного тултипа.
            if (activeEl.dataset.tmTitle != null) {
                activeEl.setAttribute('title', activeEl.dataset.tmTitle);
                delete activeEl.dataset.tmTitle;
            }
            activeEl = null;
        }
    }

    function showTip(el) {
        var text = (el.getAttribute('title') || el.textContent || '').trim();
        if (!text) return;
        activeEl = el;
        // Снимаем нативный title, чтобы не было дублирующего тултипа с задержкой.
        if (el.hasAttribute('title')) {
            el.dataset.tmTitle = el.getAttribute('title');
            el.removeAttribute('title');
        }
        tip = document.createElement('div');
        tip.className = 'tm-tooltip';
        tip.textContent = text;
        document.body.appendChild(tip);

        var r = el.getBoundingClientRect();
        var tr = tip.getBoundingClientRect();
        var top = r.bottom + 6;
        if (top + tr.height > window.innerHeight - 4) top = r.top - tr.height - 6;
        var leftPos = Math.min(r.left, window.innerWidth - tr.width - 8);
        tip.style.top = Math.max(4, top) + 'px';
        tip.style.left = Math.max(4, leftPos) + 'px';
    }

    document.addEventListener('mouseover', function (e) {
        var el = e.target.closest ? e.target.closest(SELECTOR) : null;
        if (!el || el === activeEl) return;
        hideTip();
        if (isTruncated(el)) showTip(el);
    });

    document.addEventListener('mouseout', function (e) {
        if (!activeEl) return;
        var el = e.target.closest ? e.target.closest(SELECTOR) : null;
        if (el === activeEl) hideTip();
    });

    // Тултип привязан к позиции строки — скрываем при скролле/уходе.
    window.addEventListener('scroll', hideTip, true);
    window.addEventListener('resize', hideTip);

    function init() { initResizer(); }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
