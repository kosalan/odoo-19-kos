/** @odoo-module */

import { Component, useState, onMounted } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { TipPoolReceipt } from "@pos_kitchen_reporting/js/tip_pool_receipt";

function todayISO() {
    const d = new Date();
    return d.toISOString().slice(0, 10);
}

function startOfMonthISO() {
    const d = new Date();
    d.setDate(1);
    return d.toISOString().slice(0, 10);
}

export class TipPoolView extends Component {
    static template = "pos_kitchen_reporting.TipPoolView";
    static props = {};

    setup() {
        this.state = useState({
            startDate: startOfMonthISO(),
            endDate: todayISO(),
            data: null,
            loading: false,
            lastSettlement: null,
            settlementName: "",
            creating: false,
            createdMsg: null,
            printing: false,
        });
        try {
            this.printer = useService("printer");
        } catch {
            this.printer = null;
        }
        onMounted(() => this.load());
    }

    async print() {
        if (this.state.printing || !this.state.data) {
            return;
        }
        this.state.printing = true;
        try {
            if (this.printer && typeof this.printer.print === "function") {
                await this.printer.print(
                    TipPoolReceipt,
                    { data: this.state.data },
                    { webPrintFallback: true }
                );
            } else {
                window.print();
            }
        } catch (e) {
            console.error("Print failed", e);
        } finally {
            this.state.printing = false;
        }
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.data = await rpc("/pos/reporting/tip_pool", {
                start_date: this.state.startDate,
                end_date: this.state.endDate,
            });
            const ls = await rpc("/pos/reporting/last_settlement");
            this.state.lastSettlement = ls.settlement;
        } finally {
            this.state.loading = false;
        }
    }

    quickRange(kind) {
        const d = new Date();
        const end = todayISO();
        let start;
        if (kind === "today") {
            start = end;
        } else if (kind === "week") {
            const w = new Date();
            w.setDate(w.getDate() - 6);
            start = w.toISOString().slice(0, 10);
        } else if (kind === "month") {
            start = startOfMonthISO();
        }
        this.state.startDate = start;
        this.state.endDate = end;
        this.load();
    }

    async createSettlement() {
        if (!this.state.settlementName.trim()) {
            this.state.createdMsg = { type: "error", text: "Settlement name required" };
            return;
        }
        this.state.creating = true;
        this.state.createdMsg = null;
        try {
            const res = await rpc("/pos/reporting/create_settlement", {
                name: this.state.settlementName,
                start_date: this.state.startDate,
                end_date: this.state.endDate,
            });
            if (res.error) {
                this.state.createdMsg = { type: "error", text: res.error };
            } else {
                this.state.createdMsg = {
                    type: "success",
                    text: `Draft settlement created. Open in backend to review and distribute.`,
                };
                this.state.settlementName = "";
            }
        } finally {
            this.state.creating = false;
        }
    }

    fmt(n) {
        const sym = this.state.data?.currency?.symbol || "$";
        const pos = this.state.data?.currency?.position || "before";
        const v = Number(n || 0).toFixed(2);
        return pos === "after" ? `${v} ${sym}` : `${sym}${v}`;
    }
}
