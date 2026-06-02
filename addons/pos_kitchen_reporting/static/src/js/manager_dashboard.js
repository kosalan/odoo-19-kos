/** @odoo-module */

import { Component, useState, onMounted } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { EmployeeReport } from "@pos_kitchen_reporting/js/employee_report";
import { TipPoolView } from "@pos_kitchen_reporting/js/tip_pool_view";
import { AttendanceReport } from "@pos_kitchen_reporting/js/attendance_report";

function todayISO() {
    const d = new Date();
    return d.toISOString().slice(0, 10);
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

export class ManagerDashboard extends Component {
    static template = "pos_kitchen_reporting.ManagerDashboard";
    static props = { onBack: Function };
    static components = { EmployeeReport, TipPoolView, AttendanceReport };

    setup() {
        this.state = useState({
            tab: "reports",
            loading: true,
            mode: "session",          // "session" | "range"
            startDate: daysAgoISO(29),
            endDate: todayISO(),
            nameFilter: "",
            session: null,
            employees: [],
            selectedReport: null,
        });
        onMounted(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            if (this.state.mode === "session") {
                const res = await rpc("/pos/reporting/session_employees");
                this.state.session = res.session;
                this.state.employees = res.employees;
            } else {
                const res = await rpc("/pos/reporting/range_employees", {
                    start_date: this.state.startDate,
                    end_date: this.state.endDate,
                    name_filter: this.state.nameFilter,
                });
                this.state.session = null;
                this.state.employees = res.employees;
            }
        } catch {
            this.state.employees = [];
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

    async openReport(emp) {
        if (this.state.mode === "session") {
            this.state.selectedReport = await rpc("/pos/reporting/my_report", {
                employee_id: emp.employee_id,
            });
        } else {
            this.state.selectedReport = await rpc("/pos/reporting/range_report", {
                employee_id: emp.employee_id,
                start_date: this.state.startDate,
                end_date: this.state.endDate,
            });
        }
    }

    closeReport() {
        this.state.selectedReport = null;
    }

    fmt(n) {
        return `$${Number(n || 0).toFixed(2)}`;
    }
}
