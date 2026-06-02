/** @odoo-module */

import { Component, useState, onMounted } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { AttendanceReportReceipt } from "@pos_kitchen_reporting/js/attendance_report_receipt";

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

export class AttendanceReport extends Component {
    static template = "pos_kitchen_reporting.AttendanceReport";
    static props = {};

    setup() {
        this.state = useState({
            startDate: daysAgoISO(29),
            endDate: todayISO(),
            nameFilter: "",
            data: null,
            loading: false,
            expanded: new Set(),
            printing: false,
        });
        try {
            this.printer = useService("printer");
        } catch {
            this.printer = null;
        }
        onMounted(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.data = await rpc("/pos/reporting/attendance_summary", {
                start_date: this.state.startDate,
                end_date: this.state.endDate,
                name_filter: this.state.nameFilter,
            });
        } catch {
            this.state.data = null;
        }
        this.state.loading = false;
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

    toggleExpand(empId) {
        if (this.state.expanded.has(empId)) {
            this.state.expanded.delete(empId);
        } else {
            this.state.expanded.add(empId);
        }
    }

    isExpanded(empId) {
        return this.state.expanded.has(empId);
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

    async print() {
        if (this.state.printing || !this.state.data) {
            return;
        }
        this.state.printing = true;
        try {
            if (this.printer && typeof this.printer.print === "function") {
                await this.printer.print(
                    AttendanceReportReceipt,
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
}
