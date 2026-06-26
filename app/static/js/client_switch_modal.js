/*
 * Alpine component for the single «Заменить клиента» modal — one shared picker
 * for every client-switch entry point: a filled template's role rows, a chain's
 * top «Клиенты всей цепочки» controls, and the per-step role buttons inside the
 * Alpine-rendered chain step cards.
 *
 * Mounted once per page (outside the HTMX-swapped panels so it survives swaps).
 * Buttons everywhere just dispatch a bubbling `open-client-switch` event with
 * `{ role, templateId, current, postUrl, target, standalone }`; this component
 * catches it on `window`, loads the client/account/card lists from the existing
 * fill endpoints (imperatively, so it works identically from server HTML and
 * from Alpine `x-for`), and POSTs the chosen ids to `postUrl`.
 *
 * `state` is keyed by the role name so the reused fill partials — which bind to
 * `state.<role>.clientId` etc. — resolve against this scope unchanged.
 */
window.clientSwitchModal = function () {
    const FILL = (tpl, kind, role, extra) =>
        '/templater/templates-htmx/' + tpl + '/fill/' + kind + '?role=' + encodeURIComponent(role) + (extra || '');
    return {
        open: false,
        role: 'sender',
        title: '',
        templateId: '',
        postUrl: '',
        target: '',
        standalone: false,
        search: '',
        // state[role] = { clientId, accountId, cardId } — single role at a time.
        state: { sender: { clientId: '', accountId: '', cardId: '' } },

        onOpen(detail) {
            const d = detail || {};
            if (!d.templateId) return;  // no source template → nothing to pick against
            this.role = d.role;
            this.title = d.title || '';
            this.templateId = d.templateId;
            this.postUrl = d.postUrl;
            this.target = d.target;
            this.standalone = !!d.standalone;
            this.search = '';
            const cur = d.current || {};
            this.state = {
                [this.role]: {
                    clientId: cur.clientId || '',
                    accountId: cur.accountId || '',
                    cardId: cur.cardId || '',
                },
            };
            this.open = true;
            this.$nextTick(() => {
                this.loadClients();
                this.loadDeps();
            });
        },
        close() {
            this.open = false;
        },
        cur() {
            return this.state[this.role];
        },
        // ---- list loading (reuses the fill endpoints + partials) ----
        loadClients() {
            const list = this.$refs.clients;
            if (!window.htmx || !list) return;
            const q = this.search ? '&q=' + encodeURIComponent(this.search) : '';
            window.htmx.ajax('GET', FILL(this.templateId, 'clients', this.role, q), { target: list, swap: 'innerHTML' });
        },
        loadDeps() {
            const cid = this.cur().clientId;
            const param = cid ? '&client_id=' + encodeURIComponent(cid) : '';
            if (window.htmx && this.$refs.accounts) {
                window.htmx.ajax('GET', FILL(this.templateId, 'accounts', this.role, param), { target: this.$refs.accounts, swap: 'innerHTML' });
            }
            if (window.htmx && this.$refs.cards) {
                window.htmx.ajax('GET', FILL(this.templateId, 'cards', this.role, param), { target: this.$refs.cards, swap: 'innerHTML' });
            }
        },
        // ---- selection (single role) ----
        selectClient(id) {
            if (this.cur().clientId === id) return;
            this.cur().clientId = id;
            this.cur().accountId = '';
            this.cur().cardId = '';
            this.$nextTick(() => this.loadDeps());
        },
        selectAccount(id) {
            this.cur().accountId = this.cur().accountId === id ? '' : id;
            if (this.cur().accountId) this.cur().cardId = '';
        },
        selectCard(id) {
            this.cur().cardId = this.cur().cardId === id ? '' : id;
            if (this.cur().cardId) this.cur().accountId = '';
        },
        pickFromList(event, kind) {
            const selectors = {
                client: 'button[data-client-id]',
                account: 'button[data-account-id]',
                card: 'button[data-card-id]',
            };
            const button = event.target.closest(selectors[kind]);
            if (!button || !event.currentTarget.contains(button)) return;
            const id = button.dataset[kind + 'Id'];
            if (!id) return;
            if (kind === 'client') this.selectClient(id);
            else if (kind === 'account') this.selectAccount(id);
            else if (kind === 'card') this.selectCard(id);
            event.stopPropagation();
        },
        // ---- apply ----
        apply() {
            const c = this.cur();
            if (!c.clientId || !window.htmx) return;
            window.htmx.ajax('POST', this.postUrl, {
                target: this.target,
                swap: 'outerHTML',
                values: {
                    role: this.role,
                    client_id: c.clientId,
                    account_id: c.accountId,
                    card_id: c.cardId,
                    standalone: this.standalone ? '1' : '0',
                },
            });
            this.close();
        },
    };
};
