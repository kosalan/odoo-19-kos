/** @odoo-module */

import { Component, useState, onMounted } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { Navbar } from "@point_of_sale/app/components/navbar/navbar";
import { LoginScreen } from "@point_of_sale/app/screens/login_screen/login_screen";
import { EmployeeReport } from "@pos_kitchen_reporting/js/employee_report";
import { ManagerDashboard } from "@pos_kitchen_reporting/js/manager_dashboard";

const PIN_LENGTH = 4;

export class ReportingKiosk extends Component {
    static template = "pos_kitchen_reporting.ReportingKiosk";
    static props = { onClose: Function };
    static components = { EmployeeReport, ManagerDashboard };

    setup() {
        this.state = useState({
            screen: "picker",              // "picker" | "pin" | "report" | "manager"
            loading: true,
            employees: [],
            selected: null,                // employee selected from picker
            pin: "",
            pinError: null,
            verifying: false,
            reportData: null,
        });
        onMounted(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.employees = await rpc("/pos/reporting/employees");
        } catch {
            this.state.employees = [];
        }
        this.state.loading = false;
    }

    onEmployeeClick(emp) {
        this.state.selected = emp;
        this.state.pin = "";
        this.state.pinError = null;
        this.state.screen = "pin";
    }

    onPinKey(key) {
        if (this.state.verifying) {
            return;
        }
        if (key === "clear") {
            this.state.pin = "";
            this.state.pinError = null;
        } else if (key === "back") {
            this.state.pin = this.state.pin.slice(0, -1);
            this.state.pinError = null;
        } else if (key === "ok") {
            this.verify();
        } else {
            if (this.state.pin.length >= PIN_LENGTH) {
                return;
            }
            this.state.pin += key;
            this.state.pinError = null;
            if (this.state.pin.length === PIN_LENGTH) {
                this.verify();
            }
        }
    }

    async verify() {
        if (!this.state.pin) {
            this.state.pinError = "Enter PIN";
            return;
        }
        this.state.verifying = true;
        try {
            const res = await rpc("/pos/reporting/verify_pin", {
                employee_id: this.state.selected.id,
                pin: this.state.pin,
            });
            if (!res.ok) {
                this.state.pinError = "Incorrect PIN";
                this.state.pin = "";
                return;
            }
            if (res.is_manager) {
                this.state.screen = "manager";
            } else {
                this.state.reportData = await rpc("/pos/reporting/my_report", {
                    employee_id: this.state.selected.id,
                });
                this.state.screen = "report";
            }
        } catch (e) {
            this.state.pinError = "Verification failed";
        } finally {
            this.state.verifying = false;
        }
    }

    backToPicker() {
        this.state.screen = "picker";
        this.state.selected = null;
        this.state.pin = "";
        this.state.pinError = null;
        this.state.reportData = null;
    }
}

export class ReportingButton extends Component {
    static template = "pos_kitchen_reporting.ReportingButton";
    static components = { ReportingKiosk };
    setup() {
        this.state = useState({ open: false });
    }
}

export class ReportingLoginButton extends Component {
    static template = "pos_kitchen_reporting.ReportingLoginButton";
    static components = { ReportingKiosk };
    setup() {
        this.state = useState({ open: false });
    }
}

Navbar.components = { ...Navbar.components, ReportingButton };
LoginScreen.components = { ...LoginScreen.components, ReportingLoginButton };
