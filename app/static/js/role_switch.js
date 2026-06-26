/*
 * Alpine component for the «Заменить клиента» popover — a one-role
 * client/account/card picker reused on the filled-template panel and inside the
 * chain panel's «Клиенты цепочки» section.
 *
 * It mirrors the fill page's role picker (app/templates/templates_reg/fill.html)
 * but for a single role, and reuses the same server partials and fill endpoints:
 *   GET /templater/templates-htmx/{templateId}/fill/clients|accounts|cards?role=…
 * The clients list is loaded lazily on first open (so a panel with many popovers
 * doesn't fire a request per popover up front); accounts/cards load declaratively
 * via the `client-changed` event, exactly like the fill page.
 *
 * `state` is keyed by the role name so the included fill partials — which bind to
 * `state.<role>.clientId` etc. — resolve against this scope unchanged.
 *
 * Defined on window so HTMX-swapped panels can reference it without a per-swap
 * <script> race; loaded once at page level.
 */
window.roleSwitch = function (config) {
    const role = config.role;
    const cur = config.current || {};
    return {
        open: false,
        _loaded: false,
        templateId: config.templateId || '',
        role: role,
        state: {
            [role]: {
                clientId: cur.clientId || '',
                accountId: cur.accountId || '',
                cardId: cur.cardId || '',
            },
        },
        toggle() {
            this.open = !this.open;
            if (this.open) this.ensureLoaded();
        },
        close() {
            this.open = false;
        },
        // Lazy first-load of the clients list, then load the current client's
        // accounts/cards by firing the same event a client click would.
        ensureLoaded() {
            if (!this._loaded && this.templateId) {
                this._loaded = true;
                const list = this.$root.querySelector("[data-list='clients']");
                if (window.htmx && list) {
                    window.htmx.ajax(
                        'GET',
                        '/templater/templates-htmx/' + this.templateId + '/fill/clients?role=' + role,
                        { target: list, swap: 'innerHTML' },
                    );
                }
            }
            this.$nextTick(() => this.fireClientChanged());
        },
        fireClientChanged() {
            // Dispatched on the root so the accounts/cards lists (hx-trigger
            // "client-changed from:closest .role-switch") reload for the client.
            this.$root.dispatchEvent(new CustomEvent('client-changed', { bubbles: true }));
        },
        selectClient(id) {
            if (this.state[role].clientId === id) return;
            this.state[role].clientId = id;
            this.state[role].accountId = '';
            this.state[role].cardId = '';
            this.$nextTick(() => this.fireClientChanged());
        },
        // Account/card are optional, mutually exclusive toggles for the role.
        selectAccount(id) {
            this.state[role].accountId = this.state[role].accountId === id ? '' : id;
            if (this.state[role].accountId) this.state[role].cardId = '';
        },
        selectCard(id) {
            this.state[role].cardId = this.state[role].cardId === id ? '' : id;
            if (this.state[role].cardId) this.state[role].accountId = '';
        },
        pickFromList(event, kind) {
            const selectors = {
                client: 'button[data-client-id]',
                account: 'button[data-account-id]',
                card: 'button[data-card-id]',
            };
            const selector = selectors[kind];
            if (!selector) return;
            const button = event.target.closest(selector);
            if (!button || !event.currentTarget.contains(button)) return;
            const id = button.dataset[kind + 'Id'];
            if (!id) return;
            if (kind === 'client') this.selectClient(id);
            else if (kind === 'account') this.selectAccount(id);
            else if (kind === 'card') this.selectCard(id);
            event.stopPropagation();
        },
    };
};
