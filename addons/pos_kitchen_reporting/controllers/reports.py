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

    # Tip pool calculation:
    #  - Eligible only if food_sales >= threshold
    #  - Pool taken from card tips first; cash tips cover any shortfall
    #  - If total tips (card + cash) cannot cover the target → no pool at all
    eligible = food_sales >= threshold and threshold > 0
    target_pool = (food_sales * pool_pct / 100.0) if eligible else 0.0

    if not eligible:
        pool_contribution = 0.0
        cash_tips_kept = total_cash_tips
        card_tips_to_payroll = total_card_tips
        tip_pool_shortfall = 0.0
        pool_paid_from = "none"
    elif total_card_tips >= target_pool:
        # Card tips fully cover the pool
        pool_contribution = target_pool
        cash_tips_kept = total_cash_tips
        card_tips_to_payroll = total_card_tips - target_pool
        tip_pool_shortfall = 0.0
        pool_paid_from = "card_tips"
    elif (total_card_tips + total_cash_tips) >= target_pool:
        # Cash tips fill the gap; pool gets fully paid
        shortfall = target_pool - total_card_tips
        pool_contribution = target_pool
        cash_tips_kept = total_cash_tips - shortfall
        card_tips_to_payroll = 0.0
        tip_pool_shortfall = shortfall
        pool_paid_from = "card_and_cash_tips"
    else:
        # Neither card nor cash tips cover — no pool contribution
        pool_contribution = 0.0
        cash_tips_kept = total_cash_tips
        card_tips_to_payroll = total_card_tips
        tip_pool_shortfall = 0.0
        pool_paid_from = "skipped_insufficient_tips"

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


def _compute_range_report(employee, start_dt, end_dt, company):
    """Aggregate the per-employee report across a date range (all sessions)."""
    food_ids = _category_with_descendants(company.tip_pool_food_category_ids)
    bar_ids = _category_with_descendants(company.tip_pool_bar_category_ids)
    threshold = company.tip_pool_threshold or 0.0
    pool_pct = company.tip_pool_pct or 0.0

    orders = request.env["pos.order"].sudo().search(
        [
            ("date_order", ">=", start_dt),
            ("date_order", "<=", end_dt),
            ("employee_id", "=", employee.id),
            ("state", "in", ("paid", "done", "invoiced")),
        ]
    )

    sales_by_method, tips_by_method = {}, {}
    cash_method_names = set()
    total_cash_tips = 0.0
    total_card_tips = 0.0

    for order in orders:
        order_tip = order.tip_amount or 0.0
        payments = order.payment_ids.filtered(lambda p: p.amount != 0)
        if not payments:
            continue
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

    eligible = food_sales >= threshold and threshold > 0
    target_pool = (food_sales * pool_pct / 100.0) if eligible else 0.0
    if not eligible:
        pool_contribution = 0.0
        cash_tips_kept = total_cash_tips
        card_tips_to_payroll = total_card_tips
        tip_pool_shortfall = 0.0
        pool_paid_from = "none"
    elif total_card_tips >= target_pool:
        pool_contribution = target_pool
        cash_tips_kept = total_cash_tips
        card_tips_to_payroll = total_card_tips - target_pool
        tip_pool_shortfall = 0.0
        pool_paid_from = "card_tips"
    elif (total_card_tips + total_cash_tips) >= target_pool:
        shortfall = target_pool - total_card_tips
        pool_contribution = target_pool
        cash_tips_kept = total_cash_tips - shortfall
        card_tips_to_payroll = 0.0
        tip_pool_shortfall = shortfall
        pool_paid_from = "card_and_cash_tips"
    else:
        pool_contribution = 0.0
        cash_tips_kept = total_cash_tips
        card_tips_to_payroll = total_card_tips
        tip_pool_shortfall = 0.0
        pool_paid_from = "skipped_insufficient_tips"

    total_cash_due_to_till = cash_sales + tip_pool_shortfall

    # Attendance within the range
    punches_qs = request.env["hr.attendance"].sudo().search(
        [
            ("employee_id", "=", employee.id),
            ("check_in", "<=", end_dt),
            "|",
            ("check_out", ">=", start_dt),
            ("check_out", "=", False),
        ],
        order="check_in asc",
    )
    now = fields.Datetime.now()
    punches = []
    total_worked_minutes = 0.0
    clipped_intervals = []
    for att in punches_qs:
        ci = att.check_in
        co = att.check_out or now
        eff_in = max(ci, start_dt)
        eff_out = min(co, end_dt)
        if eff_out > eff_in:
            worked = (eff_out - eff_in).total_seconds() / 60.0
            total_worked_minutes += worked
            clipped_intervals.append((eff_in, eff_out))
        punches.append(
            {
                "check_in": fields.Datetime.to_string(att.check_in),
                "check_out": fields.Datetime.to_string(att.check_out)
                if att.check_out
                else None,
                "minutes": round(
                    (eff_out - eff_in).total_seconds() / 60.0
                    if eff_out > eff_in
                    else 0,
                    1,
                ),
            }
        )

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
            # In range mode, "session" displays the date span instead of a session name
            "id": None,
            "name": f"{fields.Date.to_string(start_dt.date())} → {fields.Date.to_string(end_dt.date())}",
            "opened_at": fields.Datetime.to_string(start_dt),
            "closed_at": fields.Datetime.to_string(end_dt),
            "is_current": False,
            "is_range": True,
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

    # Group orders by employee and track food sales + tips (per period + per day)
    emp_food_sales = {}          # {emp_id: total_food_sales}
    emp_card_tips = {}           # {emp_id: total_card_tips}
    emp_cash_tips = {}           # {emp_id: total_cash_tips}
    daily_emp_food = {}          # {(date_str, emp_id): food_sales}
    daily_emp_card_tips = {}     # {(date_str, emp_id): card_tips}
    daily_emp_cash_tips = {}     # {(date_str, emp_id): cash_tips}
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
            emp_id = order.employee_id.id
            day_key = fields.Date.to_string(order.date_order.date())

            food_subtotal = 0.0
            for line in order.lines:
                if _line_category_matches(line, food_ids):
                    food_subtotal += line.price_subtotal_incl or 0.0
            emp_food_sales.setdefault(emp_id, 0.0)
            emp_food_sales[emp_id] += food_subtotal
            daily_emp_food.setdefault((day_key, emp_id), 0.0)
            daily_emp_food[(day_key, emp_id)] += food_subtotal

            # Attribute the tip to cash vs card using the same heuristic as the per-employee report
            order_tip = order.tip_amount or 0.0
            if order_tip:
                payments = order.payment_ids.filtered(lambda p: p.amount != 0)
                non_cash = payments.filtered(lambda p: not p.payment_method_id.is_cash_count)
                if non_cash:
                    emp_card_tips.setdefault(emp_id, 0.0)
                    emp_card_tips[emp_id] += order_tip
                    daily_emp_card_tips.setdefault((day_key, emp_id), 0.0)
                    daily_emp_card_tips[(day_key, emp_id)] += order_tip
                else:
                    emp_cash_tips.setdefault(emp_id, 0.0)
                    emp_cash_tips[emp_id] += order_tip
                    daily_emp_cash_tips.setdefault((day_key, emp_id), 0.0)
                    daily_emp_cash_tips[(day_key, emp_id)] += order_tip

    # Apply threshold AND "total tips must cover" per employee (period total)
    total_pool = 0.0
    contributors = []
    contributing_emp_ids = set()
    for emp_id, food_sales in emp_food_sales.items():
        if food_sales < threshold or threshold <= 0:
            continue
        target = food_sales * pool_pct / 100.0
        card_tips = emp_card_tips.get(emp_id, 0.0)
        cash_tips = emp_cash_tips.get(emp_id, 0.0)
        if (card_tips + cash_tips) < target:
            # Tips don't cover the target — no contribution at all
            continue
        total_pool += target
        contributing_emp_ids.add(emp_id)
        emp = request.env["hr.employee"].sudo().browse(emp_id)
        contributors.append(
            {
                "employee_id": emp.id if emp.exists() else None,
                "employee_name": emp.name if emp.exists() else "Unknown",
                "food_sales": round(food_sales, 2),
                "contribution": round(target, 2),
            }
        )

    # Daily breakdown — only for contributing employees, only show days where
    # the employee's tips that day covered the day's target.
    daily_breakdown = []
    for (day, emp_id), day_food in sorted(daily_emp_food.items(), reverse=True):
        if emp_id not in contributing_emp_ids:
            continue
        day_target = day_food * pool_pct / 100.0
        day_card = daily_emp_card_tips.get((day, emp_id), 0.0)
        day_cash = daily_emp_cash_tips.get((day, emp_id), 0.0)
        if (day_card + day_cash) < day_target:
            continue
        emp = request.env["hr.employee"].sudo().browse(emp_id)
        daily_breakdown.append(
            {
                "date": day,
                "employee_id": emp_id,
                "employee_name": emp.name if emp.exists() else "Unknown",
                "food_sales": round(day_food, 2),
                "contribution": round(day_target, 2),
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

    @http.route("/pos/reporting/check_attendance", type="jsonrpc", auth="user")
    def check_attendance(self, employee_id):
        """Quick status check — used by the register login flow."""
        emp = request.env["hr.employee"].sudo().browse(int(employee_id))
        if not emp.exists():
            return {"ok": False, "error": "not_found"}
        is_manager = _is_manager(emp.user_id) if emp.user_id else False
        open_att = request.env["hr.attendance"].sudo().search(
            [("employee_id", "=", emp.id), ("check_out", "=", False)], limit=1
        )
        return {
            "ok": True,
            "is_manager": is_manager,
            "clocked_in": bool(open_att),
            "employee_name": emp.name,
        }

    @http.route("/pos/reporting/clock_in", type="jsonrpc", auth="user")
    def clock_in(self, employee_id):
        """Create an open attendance for the employee. PIN check skipped —
        the caller has already authenticated the employee (e.g. via cashier login)."""
        emp = request.env["hr.employee"].sudo().browse(int(employee_id))
        if not emp.exists():
            return {"ok": False, "error": "not_found"}
        open_att = request.env["hr.attendance"].sudo().search(
            [("employee_id", "=", emp.id), ("check_out", "=", False)], limit=1
        )
        if open_att:
            return {"ok": True, "already_in": True}
        request.env["hr.attendance"].sudo().create(
            {"employee_id": emp.id, "check_in": fields.Datetime.now()}
        )
        return {"ok": True, "already_in": False}

    @http.route("/pos/reporting/clock_out", type="jsonrpc", auth="user")
    def clock_out(self, employee_id, pin=None):
        """Close any open attendance for the given employee, after PIN check."""
        emp = request.env["hr.employee"].sudo().browse(int(employee_id))
        if not emp.exists():
            return {"ok": False, "error": "Employee not found"}
        if request.env.company.attendance_kiosk_use_pin:
            if not pin:
                return {"ok": False, "error": "pin_required"}
            if emp.pin != pin:
                return {"ok": False, "error": "wrong_pin"}
        open_att = request.env["hr.attendance"].sudo().search(
            [("employee_id", "=", emp.id), ("check_out", "=", False)], limit=1
        )
        if not open_att:
            return {"ok": True, "already_out": True}
        open_att.write({"check_out": fields.Datetime.now()})
        return {"ok": True, "already_out": False}

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

    @http.route("/pos/reporting/sales_dashboard", type="jsonrpc", auth="user")
    def sales_dashboard(self, mode="session", session_id=None, start_date=None, end_date=None):
        """Aggregated sales / tips / pool / product data for the manager dashboard.
        mode='session' → current/open session.
        mode='range' → all orders between start_date and end_date."""
        company = request.env.company
        if mode == "session":
            if session_id:
                session = request.env["pos.session"].sudo().browse(int(session_id))
            else:
                session = request.env["pos.session"].sudo().search(
                    [("state", "!=", "closed")], order="start_at desc", limit=1
                )
            if not session:
                return {"empty": True}
            orders = request.env["pos.order"].sudo().search(
                [
                    ("session_id", "=", session.id),
                    ("state", "in", ("paid", "done", "invoiced")),
                ]
            )
            context = {
                "mode": "session",
                "session_name": session.name,
                "opened_at": fields.Datetime.to_string(session.start_at),
                "closed_at": fields.Datetime.to_string(session.stop_at) if session.stop_at else None,
                "is_open": session.state != "closed",
            }
        else:
            start = fields.Date.from_string(start_date)
            end = fields.Date.from_string(end_date)
            start_dt = datetime.combine(start, datetime.min.time())
            end_dt = datetime.combine(end, datetime.max.time())
            orders = request.env["pos.order"].sudo().search(
                [
                    ("date_order", ">=", start_dt),
                    ("date_order", "<=", end_dt),
                    ("state", "in", ("paid", "done", "invoiced")),
                ]
            )
            context = {
                "mode": "range",
                "period_start": fields.Date.to_string(start),
                "period_end": fields.Date.to_string(end),
            }

        food_ids = _category_with_descendants(company.tip_pool_food_category_ids)
        bar_ids = _category_with_descendants(company.tip_pool_bar_category_ids)
        threshold = company.tip_pool_threshold or 0.0
        pool_pct = company.tip_pool_pct or 0.0

        # ── Aggregations
        method_totals = {}                # {method: {sales, tips}}
        emp_data = {}                     # {emp_id: {…}}
        product_data = {}                 # {product_id: {name, qty, amount, category}}
        total_gross = 0.0
        total_tax = 0.0
        total_tip_cash = 0.0
        total_tip_card = 0.0
        order_count = 0

        for order in orders:
            order_count += 1
            order_tip = order.tip_amount or 0.0
            total_tax += (order.amount_tax or 0.0)
            total_gross += (order.amount_total or 0.0) - order_tip

            # Employee bucket
            emp = order.employee_id
            emp_key = emp.id if emp else 0
            emp_name = emp.name if emp else "Unassigned"
            ed = emp_data.setdefault(
                emp_key,
                {
                    "employee_id": emp_key or None,
                    "employee_name": emp_name,
                    "department": emp.department_id.name if emp and emp.department_id else "",
                    "methods": {},     # {method: revenue}
                    "tip_cash": 0.0,
                    "tip_card": 0.0,
                    "food_sales": 0.0,
                    "bar_sales": 0.0,
                    "cash_sales": 0.0,
                    "orders": 0,
                },
            )
            ed["orders"] += 1

            # Payments
            payments = order.payment_ids.filtered(lambda p: p.amount != 0)
            non_cash = payments.filtered(lambda p: not p.payment_method_id.is_cash_count)
            cash_pays = payments.filtered(lambda p: p.payment_method_id.is_cash_count)
            if order_tip and non_cash:
                tip_recipient = non_cash.sorted("amount", reverse=True)[0]
            elif order_tip and cash_pays:
                tip_recipient = cash_pays.sorted("amount", reverse=True)[0]
            elif order_tip:
                tip_recipient = payments.sorted("amount", reverse=True)[0] if payments else None
            else:
                tip_recipient = None

            for p in payments:
                method = p.payment_method_id.name or "Unknown"
                is_cash = p.payment_method_id.is_cash_count
                tip_share = order_tip if (tip_recipient and p.id == tip_recipient.id) else 0.0
                revenue = (p.amount or 0.0) - tip_share

                method_totals.setdefault(method, {"sales": 0.0, "tips": 0.0, "is_cash": is_cash})
                method_totals[method]["sales"] += revenue
                method_totals[method]["tips"] += tip_share

                ed["methods"].setdefault(method, 0.0)
                ed["methods"][method] += revenue
                if is_cash:
                    ed["cash_sales"] += revenue
                    if tip_share:
                        ed["tip_cash"] += tip_share
                        total_tip_cash += tip_share
                elif tip_share:
                    ed["tip_card"] += tip_share
                    total_tip_card += tip_share

            # Lines (categories + products)
            for line in order.lines:
                line_total = line.price_subtotal_incl or 0.0
                if _line_category_matches(line, food_ids):
                    ed["food_sales"] += line_total
                    line_category = "Food"
                elif _line_category_matches(line, bar_ids):
                    ed["bar_sales"] += line_total
                    line_category = "Bar"
                else:
                    line_category = "Other"
                if line.product_id:
                    pid = line.product_id.id
                    pd = product_data.setdefault(
                        pid,
                        {
                            "product_id": pid,
                            "product_name": line.product_id.display_name,
                            "category": line_category,
                            "qty": 0.0,
                            "amount": 0.0,
                        },
                    )
                    pd["qty"] += line.qty or 0.0
                    pd["amount"] += line_total

        # ── Per-employee finalization (pool contribution, cash receivable)
        by_employee = []
        total_pool = 0.0
        total_cash_receivable = 0.0
        for emp_key, ed in emp_data.items():
            food = ed["food_sales"]
            cash_tips = ed["tip_cash"]
            card_tips = ed["tip_card"]
            eligible = food >= threshold and threshold > 0
            target = (food * pool_pct / 100.0) if eligible else 0.0
            if not eligible:
                pool_contribution = 0.0
                cash_tips_kept = cash_tips
                card_to_payroll = card_tips
                shortfall = 0.0
            elif card_tips >= target:
                pool_contribution = target
                cash_tips_kept = cash_tips
                card_to_payroll = card_tips - target
                shortfall = 0.0
            elif (card_tips + cash_tips) >= target:
                shortfall = target - card_tips
                pool_contribution = target
                cash_tips_kept = cash_tips - shortfall
                card_to_payroll = 0.0
            else:
                pool_contribution = 0.0
                cash_tips_kept = cash_tips
                card_to_payroll = card_tips
                shortfall = 0.0
            cash_due = ed["cash_sales"] + shortfall
            total_pool += pool_contribution
            total_cash_receivable += cash_due
            by_employee.append(
                {
                    "employee_id": ed["employee_id"],
                    "employee_name": ed["employee_name"],
                    "department": ed["department"],
                    "orders": ed["orders"],
                    "methods": [
                        {"method": k, "amount": round(v, 2)} for k, v in ed["methods"].items()
                    ],
                    "tip_cash": round(cash_tips, 2),
                    "tip_card": round(card_tips, 2),
                    "tip_total": round(cash_tips + card_tips, 2),
                    "food_sales": round(food, 2),
                    "bar_sales": round(ed["bar_sales"], 2),
                    "pool_contribution": round(pool_contribution, 2),
                    "tip_pool_shortfall": round(shortfall, 2),
                    "cash_due_to_till": round(cash_due, 2),
                    "cash_tips_kept": round(cash_tips_kept, 2),
                    "card_tips_to_payroll": round(card_to_payroll, 2),
                }
            )
        by_employee.sort(key=lambda r: -r["cash_due_to_till"])

        by_method = [
            {
                "method": k,
                "sales": round(v["sales"], 2),
                "tips": round(v["tips"], 2),
                "is_cash": v["is_cash"],
            }
            for k, v in method_totals.items()
        ]
        by_method.sort(key=lambda r: -r["sales"])

        by_product = sorted(
            (
                {
                    **pd,
                    "qty": round(pd["qty"], 2),
                    "amount": round(pd["amount"], 2),
                }
                for pd in product_data.values()
            ),
            key=lambda r: -r["amount"],
        )

        return {
            "empty": order_count == 0,
            "context": context,
            "as_of": fields.Datetime.to_string(fields.Datetime.now()),
            "totals": {
                "gross_sales": round(total_gross, 2),
                "tax": round(total_tax, 2),
                "tip_cash": round(total_tip_cash, 2),
                "tip_card": round(total_tip_card, 2),
                "tip_total": round(total_tip_cash + total_tip_card, 2),
                "pool_total": round(total_pool, 2),
                "cash_receivable": round(total_cash_receivable, 2),
                "orders": order_count,
            },
            "by_method": by_method,
            "by_employee": by_employee,
            "by_product": by_product,
            "currency": {
                "symbol": company.currency_id.symbol,
                "position": company.currency_id.position,
            },
        }

    @http.route("/pos/reporting/email_sales_dashboard", type="jsonrpc", auth="user")
    def email_sales_dashboard(self, html, subject=None, recipient=None):
        """Email the rendered HTML of the sales dashboard to the requested recipient.
        Defaults to the current user's email."""
        if not html:
            return {"ok": False, "error": "Empty body"}
        to_addr = recipient or request.env.user.email
        if not to_addr:
            return {"ok": False, "error": "No recipient email"}
        subj = subject or "Sales Dashboard Report"
        mail = request.env["mail.mail"].sudo().create(
            {
                "subject": subj,
                "body_html": html,
                "email_to": to_addr,
                "email_from": request.env.user.email or request.env.company.email or False,
            }
        )
        mail.send()
        return {"ok": True, "sent_to": to_addr}

    @http.route("/pos/reporting/attendance_summary", type="jsonrpc", auth="user")
    def attendance_summary(self, start_date, end_date, name_filter=None):
        """All employee punches in the date range, with worked/break totals."""
        start = fields.Date.from_string(start_date)
        end = fields.Date.from_string(end_date)
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())
        now = fields.Datetime.now()

        atts = request.env["hr.attendance"].sudo().search(
            [
                ("check_in", "<=", end_dt),
                "|",
                ("check_out", ">=", start_dt),
                ("check_out", "=", False),
            ],
            order="employee_id, check_in asc",
        )

        # Group by employee
        emp_data = {}  # {emp_id: {employee, punches, intervals}}
        for att in atts:
            emp = att.employee_id
            if not emp:
                continue
            ci = att.check_in
            co = att.check_out or now
            eff_in = max(ci, start_dt)
            eff_out = min(co, end_dt)
            emp_data.setdefault(
                emp.id,
                {
                    "employee_id": emp.id,
                    "employee_name": emp.name,
                    "department": emp.department_id.name if emp.department_id else "",
                    "job_title": emp.job_title or (emp.job_id.name if emp.job_id else ""),
                    "punches": [],
                    "_intervals": [],
                },
            )
            worked = (eff_out - eff_in).total_seconds() / 60.0 if eff_out > eff_in else 0.0
            emp_data[emp.id]["punches"].append(
                {
                    "check_in": fields.Datetime.to_string(att.check_in),
                    "check_out": fields.Datetime.to_string(att.check_out)
                    if att.check_out
                    else None,
                    "minutes": round(worked, 1),
                }
            )
            if eff_out > eff_in:
                emp_data[emp.id]["_intervals"].append((eff_in, eff_out))

        # Compute totals
        result = []
        name_filter = (name_filter or "").strip().lower()
        for emp_id, info in emp_data.items():
            if name_filter and name_filter not in info["employee_name"].lower():
                continue
            intervals = info.pop("_intervals")
            intervals.sort()
            total_worked = sum((b - a).total_seconds() / 60.0 for a, b in intervals)
            total_break = 0.0
            for i in range(1, len(intervals)):
                gap = (intervals[i][0] - intervals[i - 1][1]).total_seconds() / 60.0
                if gap > 0:
                    total_break += gap
            info["total_worked_minutes"] = round(total_worked, 1)
            info["total_break_minutes"] = round(total_break, 1)
            result.append(info)
        result.sort(key=lambda r: r["employee_name"])
        return {
            "period_start": fields.Date.to_string(start),
            "period_end": fields.Date.to_string(end),
            "employees": result,
        }

    @http.route("/pos/reporting/range_employees", type="jsonrpc", auth="user")
    def range_employees(self, start_date, end_date, name_filter=None):
        """List employees with activity in the given date range, optional name filter.
        Returns per-employee totals for the period (orders, sales, tips)."""
        start = fields.Date.from_string(start_date)
        end = fields.Date.from_string(end_date)
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())

        orders = request.env["pos.order"].sudo().search(
            [
                ("date_order", ">=", start_dt),
                ("date_order", "<=", end_dt),
                ("state", "in", ("paid", "done", "invoiced")),
            ]
        )
        totals = {}
        for o in orders:
            if not o.employee_id:
                continue
            totals.setdefault(
                o.employee_id.id, {"sales": 0.0, "tips": 0.0, "orders": 0}
            )
            totals[o.employee_id.id]["sales"] += o.amount_total - (o.tip_amount or 0)
            totals[o.employee_id.id]["tips"] += o.tip_amount or 0
            totals[o.employee_id.id]["orders"] += 1

        result = []
        name_filter = (name_filter or "").strip().lower()
        for emp_id, t in totals.items():
            emp = request.env["hr.employee"].sudo().browse(emp_id)
            if not emp.exists():
                continue
            if name_filter and name_filter not in (emp.name or "").lower():
                continue
            result.append(
                {
                    "employee_id": emp.id,
                    "employee_name": emp.name,
                    "orders": t["orders"],
                    "sales": round(t["sales"], 2),
                    "tips": round(t["tips"], 2),
                }
            )
        result.sort(key=lambda r: r["employee_name"])
        return {
            "period_start": fields.Date.to_string(start),
            "period_end": fields.Date.to_string(end),
            "employees": result,
        }

    @http.route("/pos/reporting/range_report", type="jsonrpc", auth="user")
    def range_report(self, employee_id, start_date, end_date):
        """Aggregated employee report across a date range (all sessions overlapping)."""
        emp = request.env["hr.employee"].sudo().browse(int(employee_id))
        if not emp.exists():
            return {"error": "Employee not found"}
        start = fields.Date.from_string(start_date)
        end = fields.Date.from_string(end_date)
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())

        return _compute_range_report(emp, start_dt, end_dt, request.env.company)

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
