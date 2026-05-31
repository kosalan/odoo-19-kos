from odoo import api, fields, models


class TipPoolSettlement(models.Model):
    _name = "pos.tip.pool.settlement"
    _description = "Tip Pool Settlement Period"
    _order = "period_end desc, id desc"

    name = fields.Char(required=True)
    period_start = fields.Date(required=True)
    period_end = fields.Date(required=True)
    currency_id = fields.Many2one(
        "res.currency", related="company_id.currency_id", store=True
    )
    company_id = fields.Many2one(
        "res.company", default=lambda self: self.env.company, required=True
    )
    total_pool = fields.Monetary(string="Pool Total", compute="_compute_total_pool", store=True)
    line_ids = fields.One2many(
        "pos.tip.pool.settlement.line", "settlement_id", string="Distributions"
    )
    state = fields.Selection(
        [("draft", "Draft"), ("distributed", "Distributed")],
        default="draft",
        required=True,
    )
    distributed_on = fields.Datetime(string="Distributed On", readonly=True)
    distributed_by = fields.Many2one("res.users", string="Distributed By", readonly=True)

    @api.depends("line_ids.amount")
    def _compute_total_pool(self):
        for rec in self:
            rec.total_pool = sum(rec.line_ids.mapped("amount"))

    def action_distribute(self):
        for rec in self:
            if rec.state != "draft":
                continue
            rec.write(
                {
                    "state": "distributed",
                    "distributed_on": fields.Datetime.now(),
                    "distributed_by": self.env.user.id,
                }
            )

    def action_reset_to_draft(self):
        for rec in self:
            rec.write(
                {
                    "state": "draft",
                    "distributed_on": False,
                    "distributed_by": False,
                }
            )


class TipPoolSettlementLine(models.Model):
    _name = "pos.tip.pool.settlement.line"
    _description = "Tip Pool Settlement Line"

    settlement_id = fields.Many2one(
        "pos.tip.pool.settlement", required=True, ondelete="cascade"
    )
    employee_id = fields.Many2one("hr.employee", required=True)
    currency_id = fields.Many2one(
        "res.currency", related="settlement_id.currency_id", store=True
    )
    hours_worked = fields.Float()
    share_pct = fields.Float(string="Share %")
    amount = fields.Monetary()
