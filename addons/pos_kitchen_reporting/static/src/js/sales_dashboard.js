/** @odoo-module */

import { Component, useState, onMounted, useRef } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { SalesDashboardReceipt } from "@pos_kitchen_reporting/js/sales_dashboard_receipt";

function todayISO() {
    return new Date().toISOString().slice(0, 10);
}
function daysAgoISO(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return d.toISOString().slice(0, 10);
}
function startOfMonthISO() {
    const d = new Date();
    d.setDate(1);
    return d.toISOString().slice(0, 10);
}

export class SalesDashboard extends Component {
    static template = "pos_kitchen_reporting.SalesDashboard";
    static props = {};

    setup() {
        this.state = useState({
            mode: "session",                     // "session" | "range"
            startDate: daysAgoISO(29),
            endDate: todayISO(),
            data: null,
            loading: false,
            printing: false,
            emailing: false,
            emailMsg: null,
        });
        try {
            this.printer = useService("printer");
        } catch {
            this.printer = null;
        }
        this.notification = useService("notification");
        this.contentRef = useRef("content");
        onMounted(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            const payload = { mode: this.state.mode };
            if (this.state.mode === "range") {
                payload.start_date = this.state.startDate;
                payload.end_date = this.state.endDate;
            }
            this.state.data = await rpc("/pos/reporting/sales_dashboard", payload);
        } catch {
            this.state.data = null;
        }
        this.state.loading = false;
    }

    setMode(mode) {
        this.state.mode = mode;
        this.load();
    }

    quickRange(kind) {
        if (kind === "today") {
            this.state.startDate = todayISO();
            this.state.endDate = todayISO();
        } else if (kind === "week") {
            this.state.startDate = daysAgoISO(6);
            this.state.endDate = todayISO();
        } else if (kind === "30d") {
            this.state.startDate = daysAgoISO(29);
            this.state.endDate = todayISO();
        } else if (kind === "month") {
            this.state.startDate = startOfMonthISO();
            this.state.endDate = todayISO();
        }
        this.load();
    }

    fmt(n) {
        const sym = this.state.data?.currency?.symbol || "$";
        const v = Number(n || 0).toFixed(2);
        return `${sym}${v}`;
    }

    methodShare(amount) {
        const total = (this.state.data?.by_method || []).reduce(
            (acc, r) => acc + (r.sales || 0),
            0
        );
        if (!total) {
            return 0;
        }
        return Math.round((amount / total) * 100);
    }

    async print() {
        if (this.state.printing || !this.state.data) {
            return;
        }
        this.state.printing = true;
        try {
            if (this.printer && typeof this.printer.print === "function") {
                await this.printer.print(
                    SalesDashboardReceipt,
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

    async email() {
        if (this.state.emailing || !this.state.data || !this.contentRef.el) {
            return;
        }
        this.state.emailing = true;
        this.state.emailMsg = null;
        try {
            const html = this.contentRef.el.outerHTML;
            const subj = `Sales Dashboard — ${this.state.data.context.session_name ||
                `${this.state.data.context.period_start} → ${this.state.data.context.period_end}`}`;
            const res = await rpc("/pos/reporting/email_sales_dashboard", {
                html: `<html><body style="font-family: Arial, sans-serif;">${html}</body></html>`,
                subject: subj,
            });
            if (res.ok) {
                this.state.emailMsg = { type: "success", text: `Sent to ${res.sent_to}` };
            } else {
                this.state.emailMsg = { type: "error", text: res.error || "Failed to send" };
            }
        } catch (e) {
            this.state.emailMsg = { type: "error", text: "Email failed" };
        } finally {
            this.state.emailing = false;
        }
    }
}
