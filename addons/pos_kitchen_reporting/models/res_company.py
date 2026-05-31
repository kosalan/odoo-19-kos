from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    tip_pool_pct = fields.Float(
        string="Tip Pool %",
        default=3.5,
        help="Percentage of food sales contributed to the tip pool.",
    )
    tip_pool_threshold = fields.Float(
        string="Tip Pool Threshold",
        default=250.0,
        help="Minimum food sales before a server contributes to the pool.",
    )
    tip_pool_food_category_ids = fields.Many2many(
        "pos.category",
        "company_tip_pool_food_rel",
        "company_id",
        "category_id",
        string="Food Categories",
        help="Sales in these categories (and sub-categories) count as Food.",
    )
    tip_pool_bar_category_ids = fields.Many2many(
        "pos.category",
        "company_tip_pool_bar_rel",
        "company_id",
        "category_id",
        string="Bar Categories",
        help="Sales in these categories (and sub-categories) count as Bar.",
    )
    tip_pool_back_staff_department_id = fields.Many2one(
        "hr.department",
        string="Back Staff Department",
        help="Employees in this department share the tip pool.",
    )
    auto_print_reports_on_close = fields.Boolean(
        string="Auto-print Employee Reports on Session Close",
        default=False,
    )
