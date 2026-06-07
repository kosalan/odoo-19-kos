from odoo import api, fields, models


class RestaurantFloor(models.Model):
    _inherit = "restaurant.floor"

    epson_printer_ip = fields.Char(
        string="Floor Receipt Printer (ePOS)",
        help=(
            "Local IP or serial of the Epson ePOS receipt printer to use for "
            "receipts placed on this floor. Leave blank to use the POS-wide "
            "default Epson printer."
        ),
    )

    @api.model
    def _load_pos_data_fields(self, config):
        fields_list = super()._load_pos_data_fields(config)
        if "epson_printer_ip" not in fields_list:
            fields_list = fields_list + ["epson_printer_ip"]
        return fields_list
