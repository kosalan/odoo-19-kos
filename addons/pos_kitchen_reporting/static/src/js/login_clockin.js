/** @odoo-module */

import { LoginScreen } from "@point_of_sale/app/screens/login_screen/login_screen";
import { patch } from "@web/core/utils/patch";
import { rpc } from "@web/core/network/rpc";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";

/**
 * After a non-manager logs into the register, ensure they are punched in.
 * If they aren't, show a forced "Clock In" confirmation. If they cancel,
 * roll back the cashier so they can't access the POS.
 */
patch(LoginScreen.prototype, {
    async selectCashier(pin = false, login = false, list = false) {
        const previousCashier = this.pos.cashier;
        const result = await super.selectCashier(pin, login, list);
        if (login && this.pos.cashier && this.pos.cashier !== previousCashier) {
            await this._enforceClockIn(previousCashier);
        }
        return result;
    },

    async _enforceClockIn(previousCashier) {
        const cashier = this.pos.cashier;
        if (!cashier?.id) {
            return;
        }
        let status;
        try {
            status = await rpc("/pos/reporting/check_attendance", {
                employee_id: cashier.id,
            });
        } catch {
            return; // network problem — don't block login
        }
        if (!status?.ok || status.is_manager || status.clocked_in) {
            return; // manager OR already clocked in
        }

        // Force the user to confirm clock-in
        const confirmed = await new Promise((resolve) => {
            this.env.services.dialog.add(ConfirmationDialog, {
                title: _t("Clock In Required"),
                body: _t(
                    "%s, you must clock in before using the register. Click Clock In to start your shift.",
                    cashier.name
                ),
                confirmLabel: _t("Clock In"),
                cancelLabel: _t("Cancel"),
                confirm: () => resolve(true),
                cancel: () => resolve(false),
                dismiss: () => resolve(false),
            });
        });

        if (!confirmed) {
            // Roll back: log them out
            if (previousCashier) {
                this.pos.setCashier(previousCashier);
            } else {
                this.pos.resetCashier();
            }
            this.pos.hasLoggedIn = false;
            this.pos.router.navigate("LoginScreen");
            return;
        }

        try {
            await rpc("/pos/reporting/clock_in", { employee_id: cashier.id });
            this.env.services.notification.add(
                _t("Clocked in: %s", cashier.name),
                { type: "success" }
            );
        } catch (e) {
            console.error("Clock-in failed", e);
            this.env.services.notification.add(_t("Clock-in failed"), {
                type: "danger",
            });
        }
    },
});
