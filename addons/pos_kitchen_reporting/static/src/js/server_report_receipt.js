/** @odoo-module */

import { Component } from "@odoo/owl";

/**
 * Receipt-friendly rendering of a server report, used by the POS printer service.
 * The POS printer renders this offscreen and ships it to the receipt printer.
 */
export class ServerReportReceipt extends Component {
    static template = "pos_kitchen_reporting.ServerReportReceipt";
    static props = { data: Object };

    fmt(n) {
        const sym = this.props.data.currency?.symbol || "$";
        const v = Number(n || 0).toFixed(2);
        return `${sym}${v}`;
    }

    minutesToHm(m) {
        const total = Number(m) || 0;
        if (total < 1) {
            const s = Math.round(total * 60);
            return s <= 0 ? "—" : `${s}s`;
        }
        const h = Math.floor(total / 60);
        const min = Math.round(total % 60);
        return h === 0 ? `${min}m` : `${h}h ${min}m`;
    }

    formatDt(s) {
        if (!s) {
            return "—";
        }
        const d = new Date(s.replace(" ", "T") + "Z");
        return d.toLocaleString();
    }
}
