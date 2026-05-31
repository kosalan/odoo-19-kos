/** @odoo-module */

import { Component, useState, onMounted } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { Navbar } from "@point_of_sale/app/components/navbar/navbar";
import { LoginScreen } from "@point_of_sale/app/screens/login_screen/login_screen";

export class AttendanceKiosk extends Component {
    static template = "pos_kitchen_lock.AttendanceKiosk";
    static props = { onClose: Function };

    setup() {
        this.state = useState({
            employees: [],
            loading: true,
            toast: null,
            usePin: false,
            pinDialog: null, // { employee, pin, error }
        });
        onMounted(() => this.init());
    }

    async init() {
        try {
            const config = await rpc("/pos/attendance/config");
            this.state.usePin = !!config.use_pin;
        } catch {
            this.state.usePin = false;
        }
        await this.load();
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.employees = await rpc("/pos/attendance/employees");
        } catch {
            this.state.employees = [];
        }
        this.state.loading = false;
    }

    onEmployeeClick(emp) {
        if (this.state.usePin) {
            this.state.pinDialog = { employee: emp, pin: "", error: null };
        } else {
            this.submitAction(emp, null);
        }
    }

    async submitAction(emp, pin) {
        const result = await rpc("/pos/attendance/action", {
            employee_id: emp.id,
            pin: pin,
        });
        if (result.error === "wrong_pin") {
            this.state.pinDialog.error = "Incorrect PIN";
            this.state.pinDialog.pin = "";
            return;
        }
        if (result.error === "pin_required") {
            this.state.pinDialog = { employee: emp, pin: "", error: "PIN required" };
            return;
        }
        this.state.pinDialog = null;
        this.state.toast = result;
        await this.load();
        setTimeout(() => {
            this.state.toast = null;
        }, 2500);
    }

    onPinKey(key) {
        if (!this.state.pinDialog) {
            return;
        }
        if (key === "clear") {
            this.state.pinDialog.pin = "";
            this.state.pinDialog.error = null;
        } else if (key === "back") {
            this.state.pinDialog.pin = this.state.pinDialog.pin.slice(0, -1);
            this.state.pinDialog.error = null;
        } else if (key === "ok") {
            this.submitAction(this.state.pinDialog.employee, this.state.pinDialog.pin);
        } else {
            this.state.pinDialog.pin += key;
            this.state.pinDialog.error = null;
        }
    }

    closePinDialog() {
        this.state.pinDialog = null;
    }

    get checkedInCount() {
        return this.state.employees.filter((e) => e.is_checked_in).length;
    }
}

export class AttendanceButton extends Component {
    static template = "pos_kitchen_lock.AttendanceButton";
    static components = { AttendanceKiosk };

    setup() {
        this.state = useState({ open: false });
    }
}

// Large login-screen variant — same size as Open Register
export class AttendanceLoginButton extends Component {
    static template = "pos_kitchen_lock.AttendanceLoginButton";
    static components = { AttendanceKiosk };

    setup() {
        this.state = useState({ open: false });
    }
}

// Register components so the inheriting templates can reference them
Navbar.components = { ...Navbar.components, AttendanceButton };
LoginScreen.components = { ...LoginScreen.components, AttendanceLoginButton };
