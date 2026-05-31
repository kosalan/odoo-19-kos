from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request


def _category_with_descendants(category_ids):
    """Given a recordset of pos.category, return ids including all descendants."""
    if not category_ids:
        return set()
    result = set(category_ids.ids)
    to_explore = list(category_ids)
    while to_explore:
        cat = to_explore.pop()
        children = request.env["pos.category"].sudo().search(
            [("parent_id", "=", cat.id)]
        )
        for child in children:
            if child.id not in result:
                result.add(child.id)
                to_explore.append(child)
    return result


def _line_category_matches(line, category_ids):
    """True if any of the product's pos categories (or ancestors) is in category_ids."""
    if not category_ids:
        return False
    product = line.product_id
    if not product:
        return False
    for cat in product.pos_categ_ids:
        node = cat
        while node:
            if node.id in category_ids:
                return True
            node = node.parent_id
    return False


def _is_manager(user):
    return user.has_group("point_of_sale.group_pos_manager") or user.has_group(
        "base.group_system"
    )


def _compute_employee_report(employee, session, company):
    """Build the full report dict for one employee within one pos.session."""
    food_ids = _category_with_descendants(company.tip_pool_food_category_ids)
    bar_ids = _category_with_descendants(company.tip_pool_bar_category_ids)
    threshold = company.tip_pool_threshold or 0.0
    pool_pct = company.tip_pool_pct or 0.0

    # Orders for this employee within the session.
    # pos_hr stores the actual cashier in pos.order.employee_id; user_id is the session opener.
    orders = request.env["pos.order"].sudo().search(
        [
            ("session_id", "=", session.id),
            ("employee_id", "=", employee.id),
            ("state", "in", ("paid", "done", "invoiced")),
        ]
    )

    sales_by_method = {}  # {method_name: revenue_amount}
    tips_by_method = {}   # {method_name: tip_amount}
    cash_method_names = set()
    total_cash_tips = 0.0
    total_card_tips = 0.0

    for order in orders:
        order_tip = order.tip_amount or 0.0
        payments = order.payment_ids.filtered(lambda p: p.amount != 0)
        if not payments:
            continue

        # Determine which payment received the tip: prefer non-cash, else cash, else largest.
        non_cash = payments.filtered(lambda p: not p.payment_method_id.is_cash_count)
        cash_pays = payments.filtered(lambda p: p.payment_method_id.is_cash_count)
        if order_tip and non_cash:
            tip_recipient = non_cash.sorted("amount", reverse=True)[0]
        elif order_tip and cash_pays:
            tip_recipient = cash_pays.sorted("amount", reverse=True)[0]
        elif order_tip:
            tip_recipient = payments.sorted("amount", reverse=True)[0]
        else:
            tip_recipient = None

        for p in payments:
            method_name = p.payment_method_id.name or "Unknown"
            is_cash = p.payment_method_id.is_cash_count
            if is_cash:
                cash_method_names.add(method_name)
            tip_share = order_tip if (tip_recipient and p.id == tip_recipient.id) else 0.0
            revenue = (p.amount or 0.0) - tip_share
            sales_by_method[method_name] = sales_by_method.get(method_name, 0.0) + revenue
            tips_by_method[method_name] = tips_by_method.get(method_name, 0.0) + tip_share
            if tip_share:
                if is_cash:
                    total_cash_tips += tip_share
                else:
                    total_card_tips += tip_share

    # Category breakdown
    food_sales = 0.0
    bar_sales = 0.0
    for order in orders:
        for line in order.lines:
            line_total = line.price_subtotal_incl or 0.0
            if _line_category_matches(line, food_ids):
                food_sales += line_total
            elif _line_category_matches(line, bar_ids):
                bar_sales += line_total

    total_sales_revenue = sum(sales_by_method.values())
    cash_sales = sum(v for k, v in sales_by_method.items() if k in cash_method_names)

    # Tip pool calculation
    eligible = food_sales >= threshold and threshold > 0
    pool_contribution = (food_sales * pool_pct / 100.0) if eligible else 0.0

    if not eligible:
        cash_tips_kept = total_cash_tips
        card_tips_to_payroll = total_card_tips
        tip_pool_shortfall = 0.0
        pool_paid_from = "none"
    elif total_card_tips >= pool_contribution:
        # Pool fully covered by card tips; remainder to payroll
        cash_tips_kept = total_cash_tips
        card_tips_to_payroll = total_card_tips - pool_contribution
        tip_pool_shortfall = 0.0
        pool_paid_from = "card_tips"
    else:
        # Card tips insufficient — waitress pays shortfall in cash
        cash_tips_kept = total_cash_tips
        card_tips_to_payroll = 0.0
        tip_pool_shortfall = pool_contribution - total_card_tips
        pool_paid_from = "cash_owed"

    total_cash_due_to_till = cash_sales + tip_pool_shortfall

    # Attendance — any punch that OVERLAPS with the session window.
    # (check_in <= session_end) AND (check_out IS NULL OR check_out >= session_start)
    session_start = session.start_at
    now = fields.Datetime.now()
    session_end = session.stop_at or now
    punches_qs = request.env["hr.attendance"].sudo().search(
        [
            ("employee_id", "=", employee.id),
            ("check_in", "<=", session_end),
            "|",
            ("check_out", ">=", session_start),
            ("check_out", "=", False),
        ],
        order="check_in asc",
    )
    punches = []
    total_worked_minutes = 0.0
    clipped_intervals = []  # (start, end) of worked time inside session
    for att in punches_qs:
        ci = att.check_in
        co = att.check_out or now
        # Clip to session window
        eff_in = max(ci, session_start)
        eff_out = min(co, session_end)
        if eff_out > eff_in:
            worked = (eff_out - eff_in).total_seconds() / 60.0
            total_worked_minutes += worked
            clipped_intervals.append((eff_in, eff_out))
        punches.append(
            {
                "check_in": fields.Datetime.to_string(att.check_in),
                "check_out": fields.Datetime.to_string(att.check_out) if att.check_out else None,
                "minutes": round(
                    (eff_out - eff_in).total_seconds() / 60.0
                    if eff_out > eff_in
                    else 0,
                    1,
                ),
            }
        )

    # Break time = gaps between consecutive clipped intervals (still inside session)
    total_break_minutes = 0.0
    for i in range(1, len(clipped_intervals)):
        gap_start = clipped_intervals[i - 1][1]
        gap_end = clipped_intervals[i][0]
        if gap_end > gap_start:
            total_break_minutes += (gap_end - gap_start).total_seconds() / 60.0

    return {
        "employee": {
            "id": employee.id,
            "name": employee.name,
            "job_title": employee.job_title or (employee.job_id.name if employee.job_id else ""),
            "department": employee.department_id.name if employee.department_id else "",
        },
        "session": {
            "id": session.id,
            "name": session.name,
            "opened_at": fields.Datetime.to_string(session.start_at),
            "closed_at": fields.Datetime.to_string(session.stop_at) if session.stop_at else None,
            "is_current": session.state != "closed",
        },
        "currency": {
            "symbol": company.currency_id.symbol,
            "position": company.currency_id.position,
        },
        "attendance": {
            "punches": punches,
            "total_worked_minutes": round(total_worked_minutes, 1),
            "total_break_minutes": round(total_break_minutes, 1),
        },
        "sales_by_method": [
            {"method": k, "amount": round(v, 2)} for k, v in sales_by_method.items()
        ],
        "total_sales": round(total_sales_revenue, 2),
        "tips_by_method": [
            {"method": k, "amount": round(v, 2)} for k, v in tips_by_method.items() if v
        ],
        "total_tips": round(total_cash_tips + total_card_tips, 2),
        "total_cash_tips": round(total_cash_tips, 2),
        "total_card_tips": round(total_card_tips, 2),
        "categories": {
            "food": round(food_sales, 2),
            "bar": round(bar_sales, 2),
        },
        "payout": {
            "food_sales": round(food_sales, 2),
            "threshold": round(threshold, 2),
            "pool_pct": pool_pct,
            "eligible_for_pool": eligible,
            "pool_contribution": round(pool_contribution, 2),
            "cash_sales": round(cash_sales, 2),
            "tip_pool_shortfall": round(tip_pool_shortfall, 2),
            "total_cash_due_to_till": round(total_cash_due_to_till, 2),
            "cash_tips_kept": round(cash_tips_kept, 2),
            "card_tips_to_payroll": round(card_tips_to_payroll, 2),
            "pool_paid_from": pool_paid_from,
        },
    }


def _compute_tip_pool_for_range(company, start_date, end_date):
    """Sum pool contributions across all closed/in-progress sessions in the date range."""
    food_ids = _category_with_descendants(company.tip_pool_food_category_ids)
    threshold = company.tip_pool_threshold or 0.0
    pool_pct = company.tip_pool_pct or 0.0

    # Find sessions overlapping the period
    sessions = request.env["pos.session"].sudo().search(
        [
            ("start_at", "<=", end_date),
            "|",
            ("stop_at", ">=", start_date),
            ("stop_at", "=", False),
        ]
    )

    # Group orders by employee (period total) and by (day, employee) for daily breakdown
    emp_food_sales = {}            # {employee_id: total_food_sales}
    daily_emp_food = {}            # {(date_str, employee_id): food_sales}
    for session in sessions:
        orders = request.env["pos.order"].sudo().search(
            [
                ("session_id", "=", session.id),
                ("date_order", ">=", start_date),
                ("date_order", "<=", end_date),
                ("state", "in", ("paid", "done", "invoiced")),
            ]
        )
        for order in orders:
            if not order.employee_id:
                continue
            food_subtotal = 0.0
            for line in order.lines:
                if _line_category_matches(line, food_ids):
                    food_subtotal += line.price_subtotal_incl or 0.0
            emp_food_sales.setdefault(order.employee_id.id, 0.0)
            emp_food_sales[order.employee_id.id] += food_subtotal
            day_key = fields.Date.to_string(order.date_order.date())
            daily_emp_food.setdefault((day_key, order.employee_id.id), 0.0)
            daily_emp_food[(day_key, order.employee_id.id)] += food_subtotal

    # Apply threshold + pool % per employee (period total)
    total_pool = 0.0
    contributors = []
    for emp_id, food_sales in emp_food_sales.items():
        if food_sales < threshold or threshold <= 0:
            continue
        contribution = food_sales * pool_pct / 100.0
        total_pool += contribution
        emp = request.env["hr.employee"].sudo().browse(emp_id)
        contributors.append(
            {
                "employee_id": emp.id if emp.exists() else None,
                "employee_name": emp.name if emp.exists() else "Unknown",
                "food_sales": round(food_sales, 2),
                "contribution": round(contribution, 2),
            }
        )

    # Build daily-contribution breakdown — only show days where the employee
    # also qualified for the period (i.e., they are in contributors)
    contributing_emp_ids = {c["employee_id"] for c in contributors}
    daily_breakdown = []
    for (day, emp_id), day_food in sorted(daily_emp_food.items(), reverse=True):
        if emp_id not in contributing_emp_ids:
            continue
        emp = request.env["hr.employee"].sudo().browse(emp_id)
        day_contribution = day_food * pool_pct / 100.0
        daily_breakdown.append(
            {
                "date": day,
                "employee_id": emp_id,
                "employee_name": emp.name if emp.exists() else "Unknown",
                "food_sales": round(day_food, 2),
                "contribution": round(day_contribution, 2),
            }
        )

    # Back-staff distribution
    dept = company.tip_pool_back_staff_department_id
    back_staff = (
        request.env["hr.employee"].sudo().search([("department_id", "=", dept.id)])
        if dept
        else request.env["hr.employee"].sudo().browse([])
    )

    # Sum hours worked by each back-staff employee in the period
    staff_lines = []
    total_back_hours = 0.0
    for emp in back_staff:
        atts = request.env["hr.attendance"].sudo().search(
            [
                ("employee_id", "=", emp.id),
                ("check_in", ">=", start_date),
                ("check_in", "<=", end_date),
            ]
        )
        emp_minutes = 0.0
        for att in atts:
            co = att.check_out or fields.Datetime.now()
            emp_minutes += (co - att.check_in).total_seconds() / 60.0
        emp_hours = emp_minutes / 60.0
        total_back_hours += emp_hours
        staff_lines.append(
            {
                "employee_id": emp.id,
                "employee_name": emp.name,
                "hours": round(emp_hours, 2),
            }
        )

    # Compute share %
    for line in staff_lines:
        if total_back_hours > 0:
            line["share_pct"] = round(line["hours"] / total_back_hours * 100.0, 2)
            line["amount"] = round(total_pool * line["hours"] / total_back_hours, 2)
        else:
            line["share_pct"] = 0.0
            line["amount"] = 0.0

    return {
        "period_start": fields.Date.to_string(start_date) if isinstance(start_date, (datetime,)) else str(start_date),
        "period_end": fields.Date.to_string(end_date) if isinstance(end_date, (datetime,)) else str(end_date),
        "total_pool": round(total_pool, 2),
        "contributors": contributors,
        "daily_contributions": daily_breakdown,
        "back_staff": staff_lines,
        "total_back_hours": round(total_back_hours, 2),
        "currency": {
            "symbol": company.currency_id.symbol,
            "position": company.currency_id.position,
        },
    }


class PosReportingController(http.Controller):

    @http.route("/pos/reporting/config", type="jsonrpc", auth="user")
    def get_config(self):
        company = request.env.company
        return {
            "is_manager": _is_manager(request.env.user),
            "use_pin": company.attendance_kiosk_use_pin,
            "pool_pct": company.tip_pool_pct,
            "pool_threshold": company.tip_pool_threshold,
            "back_staff_department": company.tip_pool_back_staff_department_id.name
            if company.tip_pool_back_staff_department_id
            else None,
            "currency": {
                "symbol": company.currency_id.symbol,
                "position": company.currency_id.position,
            },
        }

    @http.route("/pos/reporting/employees", type="jsonrpc", auth="user")
    def get_employees(self):
        """List employees eligible to open the reporting kiosk.
        Includes all currently checked-in employees PLUS all managers
        (managers always shown even if not clocked in)."""
        all_emps = request.env["hr.employee"].sudo().search(
            [("active", "=", True)], order="name"
        )
        result = []
        for emp in all_emps:
            open_att = request.env["hr.attendance"].sudo().search(
                [("employee_id", "=", emp.id), ("check_out", "=", False)], limit=1
            )
            is_in = bool(open_att)
            is_manager = _is_manager(emp.user_id) if emp.user_id else False
            if not is_in and not is_manager:
                continue
            result.append(
                {
                    "id": emp.id,
                    "name": emp.name,
                    "job_title": emp.job_title
                    or (emp.job_id.name if emp.job_id else ""),
                    "department": emp.department_id.name
                    if emp.department_id
                    else "",
                    "is_checked_in": is_in,
                    "is_manager": is_manager,
                    "image": emp.image_128.decode() if emp.image_128 else False,
                    "has_pin": bool(emp.pin),
                }
            )
        return result

    @http.route("/pos/reporting/verify_pin", type="jsonrpc", auth="user")
    def verify_pin(self, employee_id, pin):
        emp = request.env["hr.employee"].sudo().browse(int(employee_id))
        if not emp.exists():
            return {"ok": False, "error": "Employee not found"}
        if request.env.company.attendance_kiosk_use_pin and emp.pin != pin:
            return {"ok": False, "error": "wrong_pin"}
        return {
            "ok": True,
            "is_manager": _is_manager(emp.user_id) if emp.user_id else False,
        }

    @http.route("/pos/reporting/identify_by_pin", type="jsonrpc", auth="user")
    def identify_by_pin(self, pin):
        """Look up an employee by PIN. Returns the matching employee + manager flag.
        This is the entry point for the reporting kiosk — PIN identifies who you are."""
        if not pin:
            return {"ok": False, "error": "PIN required"}
        emps = request.env["hr.employee"].sudo().search(
            [("pin", "=", pin), ("active", "=", True)]
        )
        if not emps:
            return {"ok": False, "error": "wrong_pin"}
        # If multiple employees share a PIN, prefer one with a user account (manager more likely)
        emp = emps.filtered(lambda e: e.user_id)[:1] or emps[:1]
        is_manager = _is_manager(emp.user_id) if emp.user_id else False
        return {
            "ok": True,
            "employee": {
                "id": emp.id,
                "name": emp.name,
                "is_manager": is_manager,
            },
        }

    @http.route("/pos/reporting/my_report", type="jsonrpc", auth="user")
    def my_report(self, employee_id, session_id=None):
        emp = request.env["hr.employee"].sudo().browse(int(employee_id))
        if not emp.exists():
            return {"error": "Employee not found"}

        if session_id:
            session = request.env["pos.session"].sudo().browse(int(session_id))
        else:
            # Default = current open session for the active config
            session = request.env["pos.session"].sudo().search(
                [("state", "!=", "closed")], order="start_at desc", limit=1
            )
        if not session:
            return {"error": "No session"}

        return _compute_employee_report(emp, session, request.env.company)

    @http.route("/pos/reporting/session_employees", type="jsonrpc", auth="user")
    def session_employees(self, session_id=None):
        """List employees who had orders in the given session (manager view)."""
        if session_id:
            session = request.env["pos.session"].sudo().browse(int(session_id))
        else:
            session = request.env["pos.session"].sudo().search(
                [("state", "!=", "closed")], order="start_at desc", limit=1
            )
        if not session:
            return {"session": None, "employees": []}

        orders = request.env["pos.order"].sudo().search(
            [
                ("session_id", "=", session.id),
                ("state", "in", ("paid", "done", "invoiced")),
            ]
        )
        emp_totals = {}
        for o in orders:
            if not o.employee_id:
                continue
            emp_totals.setdefault(o.employee_id.id, {"sales": 0.0, "tips": 0.0, "orders": 0})
            emp_totals[o.employee_id.id]["sales"] += o.amount_total - (o.tip_amount or 0)
            emp_totals[o.employee_id.id]["tips"] += o.tip_amount or 0
            emp_totals[o.employee_id.id]["orders"] += 1

        result = []
        for emp_id, totals in emp_totals.items():
            emp = request.env["hr.employee"].sudo().browse(emp_id)
            if not emp.exists():
                continue
            result.append(
                {
                    "employee_id": emp.id,
                    "employee_name": emp.name,
                    "orders": totals["orders"],
                    "sales": round(totals["sales"], 2),
                    "tips": round(totals["tips"], 2),
                }
            )
        result.sort(key=lambda r: r["employee_name"])
        return {
            "session": {
                "id": session.id,
                "name": session.name,
                "opened_at": fields.Datetime.to_string(session.start_at),
                "closed_at": fields.Datetime.to_string(session.stop_at) if session.stop_at else None,
            },
            "employees": result,
        }

    @http.route("/pos/reporting/tip_pool", type="jsonrpc", auth="user")
    def tip_pool(self, start_date, end_date):
        company = request.env.company
        start = fields.Date.from_string(start_date)
        end = fields.Date.from_string(end_date)
        # Convert to datetime span
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())
        return _compute_tip_pool_for_range(company, start_dt, end_dt)

    @http.route("/pos/reporting/create_settlement", type="jsonrpc", auth="user")
    def create_settlement(self, name, start_date, end_date):
        if not _is_manager(request.env.user):
            return {"error": "Not authorized"}
        company = request.env.company
        start = fields.Date.from_string(start_date)
        end = fields.Date.from_string(end_date)
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())
        data = _compute_tip_pool_for_range(company, start_dt, end_dt)

        settlement = request.env["pos.tip.pool.settlement"].sudo().create(
            {
                "name": name,
                "period_start": start,
                "period_end": end,
                "company_id": company.id,
            }
        )
        for line in data["back_staff"]:
            request.env["pos.tip.pool.settlement.line"].sudo().create(
                {
                    "settlement_id": settlement.id,
                    "employee_id": line["employee_id"],
                    "hours_worked": line["hours"],
                    "share_pct": line["share_pct"],
                    "amount": line["amount"],
                }
            )
        return {"ok": True, "settlement_id": settlement.id}

    @http.route("/pos/reporting/last_settlement", type="jsonrpc", auth="user")
    def last_settlement(self):
        rec = request.env["pos.tip.pool.settlement"].sudo().search(
            [("state", "=", "distributed")],
            order="distributed_on desc",
            limit=1,
        )
        if not rec:
            return {"settlement": None}
        return {
            "settlement": {
                "id": rec.id,
                "name": rec.name,
                "period_start": fields.Date.to_string(rec.period_start),
                "period_end": fields.Date.to_string(rec.period_end),
                "distributed_on": fields.Datetime.to_string(rec.distributed_on),
                "total_pool": rec.total_pool,
            }
        }
