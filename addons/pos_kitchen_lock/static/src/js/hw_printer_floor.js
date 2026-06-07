/** @odoo-module */

import { HWPrinter } from "@point_of_sale/app/utils/printer/hw_printer";
import { patch } from "@web/core/utils/patch";

/**
 * Patch the IoT-box receipt printer to include the current order's floor name.
 * The IoT box uses this to route receipt prints to the floor-specific printer.
 * Kitchen prints come from the Odoo server and are routed separately.
 */
patch(HWPrinter.prototype, {
    sendPrintingJob(img) {
        let floor = "";
        try {
            const pos = odoo.__WOWL_DEBUG__?.root?.env?.services?.pos;
            const order = pos?.getOrder?.();
            floor = order?.table_id?.floor_id?.name || "";
        } catch {
            floor = "";
        }
        return this.sendAction({
            action: "print_receipt",
            receipt: img,
            floor,
        });
    },
});
