/** @odoo-module */

import { Component, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { ServerReportReceipt } from "@pos_kitchen_reporting/js/server_report_receipt";

export class EmployeeReport extends Component {
    static template = "pos_kitchen_reporting.EmployeeReport";
    static props = {
        data: Object,
        onBack: Function,
        isManager: { type: Boolean, optional: true },
        onRefresh: { type: Function, optional: true },
    };

    setup() {
        this.state = useState({
            printing: false,
            clockOutPrompt: false,    // show "must clock out" warning
            clockOutPinDialog: false, // show PIN entry
            clockOutPin: "",
            clockOutError: null,
            clockingOut: false,
        });
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

    get hasOpenPunch() {
        return (this.props.data?.attendance?.punches || []).some((p) => !p.check_out);
    }

    async print() {
        if (this.state.printing || this.state.clockingOut) {
            return;
        }
        // Non-managers must clock out before printing if still on shift
        if (!this.props.isManager && this.hasOpenPunch) {
            this.state.clockOutPrompt = true;
            return;
        }
        await this._doPrint();
    }

    async _doPrint() {
        this.state.printing = true;
        try {
            if (this.printer && typeof this.printer.print === "function") {
                await this.printer.print(
                    ServerReportReceipt,
                    { data: this.props.data },
                    { webPrintFallback: true }
                );
            } else {
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

    confirmClockOut() {
        this.state.clockOutPrompt = false;
        this.state.clockOutPinDialog = true;
        this.state.clockOutPin = "";
        this.state.clockOutError = null;
    }

    cancelClockOut() {
        this.state.clockOutPrompt = false;
        this.state.clockOutPinDialog = false;
        this.state.clockOutPin = "";
        this.state.clockOutError = null;
    }

    onClockOutKey(key) {
        if (this.state.clockingOut) {
            return;
        }
        if (key === "clear") {
            this.state.clockOutPin = "";
            this.state.clockOutError = null;
        } else if (key === "back") {
            this.state.clockOutPin = this.state.clockOutPin.slice(0, -1);
            this.state.clockOutError = null;
        } else if (key === "ok") {
            this.submitClockOut();
        } else {
            if (this.state.clockOutPin.length >= 4) {
                return;
            }
            this.state.clockOutPin += key;
            this.state.clockOutError = null;
            if (this.state.clockOutPin.length === 4) {
                this.submitClockOut();
            }
        }
    }

    async submitClockOut() {
        if (!this.state.clockOutPin) {
            this.state.clockOutError = "Enter PIN";
            return;
        }
        this.state.clockingOut = true;
        try {
            const res = await rpc("/pos/reporting/clock_out", {
                employee_id: this.props.data.employee.id,
                pin: this.state.clockOutPin,
            });
            if (!res.ok) {
                this.state.clockOutError =
                    res.error === "wrong_pin" ? "Incorrect PIN" : "Clock out failed";
                this.state.clockOutPin = "";
                return;
            }
            this.state.clockOutPinDialog = false;
            // Refresh the report to pick up the new check_out time, then print
            if (this.props.onRefresh) {
                await this.props.onRefresh();
            }
            await this._doPrint();
        } catch (e) {
            this.state.clockOutError = "Error clocking out";
        } finally {
            this.state.clockingOut = false;
        }
    }
}
