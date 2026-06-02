/** @odoo-module */

import { Component } from "@odoo/owl";

export class AttendanceReportReceipt extends Component {
    static template = "pos_kitchen_reporting.AttendanceReportReceipt";
    static props = { data: Object };

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
