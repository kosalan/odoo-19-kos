/** @odoo-module */

import { Component, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { ServerReportReceipt } from "@pos_kitchen_reporting/js/server_report_receipt";

export class EmployeeReport extends Component {
    static template = "pos_kitchen_reporting.EmployeeReport";
    static props = {
        data: Object,
        onBack: Function,
    };

    setup() {
        this.state = useState({ printing: false });
        this.generatedAt = new Date().toLocaleString();
        try {
            this.printer = useService("printer");
        } catch {
            this.printer = null;
        }
    }

    fmt(n) {
        const sym = this.props.data.currency?.symbol || "$";
        const pos = this.props.data.currency?.position || "before";
        const v = Number(n || 0).toFixed(2);
        return pos === "after" ? `${v} ${sym}` : `${sym}${v}`;
    }

    minutesToHm(m) {
        const total = Number(m) || 0;
        if (total < 1) {
            // Less than a minute — show seconds for clarity
            const s = Math.round(total * 60);
            return s <= 0 ? "—" : `${s}s`;
        }
        const h = Math.floor(total / 60);
        const min = Math.round(total % 60);
        if (h === 0) {
            return `${min}m`;
        }
        return `${h}h ${min}m`;
    }

    formatDt(s) {
        if (!s) {
            return "—";
        }
        const d = new Date(s.replace(" ", "T") + "Z");
        return d.toLocaleString();
    }

    async print() {
        if (this.state.printing) {
            return;
        }
        this.state.printing = true;
        try {
            if (this.printer && typeof this.printer.print === "function") {
                await this.printer.print(
                    ServerReportReceipt,
                    { data: this.props.data },
                    { webPrintFallback: true }
                );
            } else {
                // Fallback: open a print window
                const w = window.open("", "_blank", "width=400,height=700");
                if (w) {
                    w.document.write(
                        `<html><head><title>Report</title></head><body>` +
                        document.querySelector(".rep-report")?.outerHTML +
                        `</body></html>`
                    );
                    w.document.close();
                    w.focus();
                    w.print();
                    setTimeout(() => w.close(), 500);
                }
            }
        } catch (e) {
            console.error("Print failed", e);
        } finally {
            this.state.printing = false;
        }
    }
}
