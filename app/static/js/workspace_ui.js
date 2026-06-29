// Workspace UI: перетаскиваемый разделитель между деревом и панелью + мгновенные
// тултипы с полным именем для обрезанных узлов дерева. Слушатели навешиваются
// только при наличии .workspace (на прочих страницах — полный no-op). Desktop-only.
(function () {
    'use strict';

    var STORAGE_KEY = 'tm:treeWidth';
    var DEFAULT_WIDTH = 320;
    var MIN_WIDTH = 240;
    var KEY_STEP = 16;          // шаг ←/→ для клавиатурного ресайза
    var CLICK_SLOP = 4;         // смещение в px, ниже которого pointerup = «клик»
    var DBLCLICK_MS = 300;

    function maxWidth() {
        return Math.min(Math.round(window.innerWidth * 0.6), 760);
    }

    function clampWidth(px) {
        return Math.max(MIN_WIDTH, Math.min(px, maxWidth()));
    }

    function setWidth(workspace, px) {
        var w = clampWidth(px);
        workspace.style.setProperty('--tree-w', w + 'px');
        return w;
    }

    function save(px) {
        try { localStorage.setItem(STORAGE_KEY, String(px)); } catch (e) {}
    }

    // ---- Resizer ----------------------------------------------------------
    function initResizer(workspace) {
        var left = workspace.querySelector('.workspace-left');
        if (!left || workspace.querySelector('.workspace-resizer')) return;

        // Реальная текущая ширина дерева — учитывает CSS clamp() на колонке,
        // поэтому надёжнее, чем парсить --tree-w.
        function currentWidth() {
            return Math.round(left.getBoundingClientRect().width) || DEFAULT_WIDTH;
        }

        var resizer = document.createElement('div');
        resizer.className = 'workspace-resizer';
        resizer.setAttribute('role', 'separator');
        resizer.setAttribute('aria-orientation', 'vertical');
        resizer.tabIndex = 0;
        resizer.setAttribute('aria-label', 'Изменить ширину дерева (стрелки ←/→, двойной клик — сброс)');
        resizer.title = 'Перетащите, чтобы изменить ширину • двойной клик — сброс';
        left.insertAdjacentElement('afterend', resizer);

        var startX = 0;
        var startW = 0;
        var lastClickAt = 0;

        function onMove(e) {
            setWidth(workspace, startW + (e.clientX - startX));
        }

        function reset() {
            setWidth(workspace, DEFAULT_WIDTH);
            try { localStorage.removeItem(STORAGE_KEY); } catch (err) {}
        }

        function onUp(e) {
            resizer.classList.remove('dragging');
            document.body.classList.remove('workspace-resizing');
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);
            window.removeEventListener('pointercancel', onUp);
            try { resizer.releasePointerCapture(e.pointerId); } catch (err) {}

            // pointercancel (прерывание ОС) не несёт осмысленного clientX —
            // считаем это отменой драга, без клик-детекта и без сохранения.
            if (e.type === 'pointercancel') return;

            var moved = Math.abs(e.clientX - startX);
            if (moved < CLICK_SLOP) {
                // Двойной клик ловим вручную по таймстампам: preventDefault на
                // pointerdown подавляет нативный dblclick в spec-совместимых
                // браузерах, поэтому полагаться на него нельзя.
                var now = Date.now();
                if (now - lastClickAt < DBLCLICK_MS) { reset(); lastClickAt = 0; }
                else lastClickAt = now;
                return;
            }
            save(currentWidth());
        }

        resizer.addEventListener('pointerdown', function (e) {
            e.preventDefault();
            startX = e.clientX;
            startW = currentWidth();
            resizer.classList.add('dragging');
            document.body.classList.add('workspace-resizing');
            try { resizer.setPointerCapture(e.pointerId); } catch (err) {}
            window.addEventListener('pointermove', onMove);
            window.addEventListener('pointerup', onUp);
            window.addEventListener('pointercancel', onUp);
        });

        // Клавиатурный ресайз: ←/→ меняют ширину, Home — сброс.
        resizer.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                e.preventDefault();
                var delta = e.key === 'ArrowLeft' ? -KEY_STEP : KEY_STEP;
                save(setWidth(workspace, currentWidth() + delta));
            } else if (e.key === 'Home') {
                e.preventDefault();
                reset();
            }
        });
    }

    // ---- Мгновенные тултипы -----------------------------------------------
    var SELECTOR = '.tree-name, .tree-folder-name, .tree-collection-name';
    var tip = null;
    var activeEl = null;

    function isTruncated(el) {
        return el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1;
    }

    // restoreTitle=false убирает только всплывашку, но оставляет title снятым:
    // на scroll/resize указатель ещё над строкой, и вернуть title значило бы
    // дать всплыть медленному нативному тултипу. title восстанавливаем только
    // на реальном уходе курсора (mouseout).
    function hideTip(restoreTitle) {
        if (tip) { tip.remove(); tip = null; }
        if (restoreTitle && activeEl) {
            if (activeEl.dataset.tmTitle != null) {
                activeEl.setAttribute('title', activeEl.dataset.tmTitle);
                delete activeEl.dataset.tmTitle;
            }
            activeEl = null;
        }
    }

    function showTip(el) {
        if (!isTruncated(el)) return;
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

    function initTooltips() {
        document.addEventListener('mouseover', function (e) {
            var el = e.target.closest ? e.target.closest(SELECTOR) : null;
            if (!el) return;
            // Тот же узел (в т.ч. переход на вложенную иконку) — не мерцаем;
            // если всплывашка была снята скроллом — показываем заново.
            if (el === activeEl) { if (!tip) showTip(el); return; }
            hideTip(true);
            showTip(el);
        });

        document.addEventListener('mouseout', function (e) {
            if (!activeEl) return;
            var el = e.target.closest ? e.target.closest(SELECTOR) : null;
            if (el !== activeEl) return;
            // Переход на потомка того же узла — не считаем уходом.
            if (e.relatedTarget && activeEl.contains(e.relatedTarget)) return;
            hideTip(true);
        });

        // Всплывашка привязана к позиции строки — убираем при скролле/ресайзе,
        // title оставляем снятым до настоящего mouseout (см. hideTip).
        window.addEventListener('scroll', function () { hideTip(false); }, true);
        window.addEventListener('resize', function () { hideTip(false); });
    }

    function init() {
        var workspace = document.querySelector('.workspace');
        if (!workspace) return;            // прочие страницы — полный no-op
        initResizer(workspace);
        initTooltips();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
