/** @odoo-module */

import { Component } from "@odoo/owl";

export class TipPoolReceipt extends Component {
    static template = "pos_kitchen_reporting.TipPoolReceipt";
    static props = { data: Object };

    fmt(n) {
        const sym = this.props.data.currency?.symbol || "$";
        const v = Number(n || 0).toFixed(2);
        return `${sym}${v}`;
    }
}
