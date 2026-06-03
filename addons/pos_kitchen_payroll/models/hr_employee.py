from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    hourly_wage = fields.Monetary(
        related="version_id.hourly_wage",
        readonly=False,
        inherited=True,
        groups="hr.group_hr_user",
    )
    monthly_wage_equivalent = fields.Monetary(
        related="version_id.monthly_wage_equivalent",
        readonly=True,
        inherited=True,
        groups="hr.group_hr_user",
    )
