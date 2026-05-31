/** @odoo-module */

import { Component, useState, onMounted } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { EmployeeReport } from "@pos_kitchen_reporting/js/employee_report";
import { TipPoolView } from "@pos_kitchen_reporting/js/tip_pool_view";

export class ManagerDashboard extends Component {
    static template = "pos_kitchen_reporting.ManagerDashboard";
    static props = { onBack: Function };
    static components = { EmployeeReport, TipPoolView };

    setup() {
        this.state = useState({
            tab: "reports",       // "reports" | "tippool"
            loading: true,
            session: null,
            employees: [],        // session contributors
            selectedReport: null, // employee report dict
        });
        onMounted(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            const res = await rpc("/pos/reporting/session_employees");
            this.state.session = res.session;
            this.state.employees = res.employees;
        } catch {
            this.state.employees = [];
        }
        this.state.loading = false;
    }

    async openReport(emp) {
        this.state.selectedReport = await rpc("/pos/reporting/my_report", {
            employee_id: emp.employee_id,
        });
    }

    closeReport() {
        this.state.selectedReport = null;
    }

    fmt(n) {
        return `$${Number(n || 0).toFixed(2)}`;
    }
}
