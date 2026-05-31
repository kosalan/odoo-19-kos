from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    tip_pool_pct = fields.Float(related="company_id.tip_pool_pct", readonly=False)
    tip_pool_threshold = fields.Float(
        related="company_id.tip_pool_threshold", readonly=False
    )
    tip_pool_food_category_ids = fields.Many2many(
        related="company_id.tip_pool_food_category_ids", readonly=False
    )
    tip_pool_bar_category_ids = fields.Many2many(
        related="company_id.tip_pool_bar_category_ids", readonly=False
    )
    tip_pool_back_staff_department_id = fields.Many2one(
        related="company_id.tip_pool_back_staff_department_id", readonly=False
    )
    auto_print_reports_on_close = fields.Boolean(
        related="company_id.auto_print_reports_on_close", readonly=False
    )
