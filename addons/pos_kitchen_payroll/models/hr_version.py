from odoo import api, fields, models


WEEKS_PER_MONTH = 52 / 12  # ≈ 4.333


class HrVersion(models.Model):
    _inherit = "hr.version"

    hourly_wage = fields.Monetary(
        string="Hourly Wage",
        tracking=True,
        aggregator="avg",
        help="Employee's gross hourly wage. The monthly wage is derived from this "
             "and the working hours of the assigned schedule.",
    )

    monthly_wage_equivalent = fields.Monetary(
        string="Monthly Equivalent",
        compute="_compute_monthly_wage_equivalent",
        help="Hourly wage × hours per week × 52 / 12.",
    )

    @api.depends("hourly_wage", "resource_calendar_id.hours_per_week")
    def _compute_monthly_wage_equivalent(self):
        for rec in self:
            hours_per_week = rec.resource_calendar_id.hours_per_week or 0.0
            rec.monthly_wage_equivalent = (
                (rec.hourly_wage or 0.0) * hours_per_week * WEEKS_PER_MONTH
            )

    @api.onchange("hourly_wage", "resource_calendar_id")
    def _onchange_hourly_wage_sync_monthly(self):
        """Keep the monthly `wage` in sync with the hourly rate so any
        downstream payroll/reporting that reads `wage` still works."""
        for rec in self:
            hours_per_week = rec.resource_calendar_id.hours_per_week or 0.0
            if rec.hourly_wage and hours_per_week:
                rec.wage = rec.hourly_wage * hours_per_week * WEEKS_PER_MONTH
