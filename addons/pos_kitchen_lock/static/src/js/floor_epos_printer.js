/** @odoo-module */

import { PrinterService } from "@point_of_sale/app/services/printer_service";
import { EpsonPrinter } from "@point_of_sale/app/utils/printer/epson_printer";
import { patch } from "@web/core/utils/patch";

/**
 * Per-floor ePOS printers.
 *
 * When a restaurant.floor has an `epson_printer_ip`, receipts placed on tables
 * in that floor are routed to that floor's printer instead of the POS-wide
 * default. Falls back to the default printer set on the POS config.
 *
 * Printers are cached so we don't reinstantiate on every print.
 */
patch(PrinterService.prototype, {
    setup(env, deps) {
        super.setup(env, deps);
        this._floorPrinters = new Map(); // floor_id → EpsonPrinter
    },

    _getFloorPrinter() {
        try {
            const pos = this.env.services.pos;
            const order = pos?.getOrder?.();
            const floor = order?.table_id?.floor_id;
            const ip = floor?.epson_printer_ip;
            if (!floor || !ip) {
                return null;
            }
            if (!this._floorPrinters.has(floor.id)) {
                this._floorPrinters.set(floor.id, new EpsonPrinter({ ip }));
            }
            return this._floorPrinters.get(floor.id);
        } catch {
            return null;
        }
    },

    async printHtml(el, options = {}) {
        const floorPrinter = this._getFloorPrinter();
        if (!floorPrinter) {
            return super.printHtml(el, options);
        }
        const result = await floorPrinter.printReceipt(el);
        if (result.successful) {
            return result;
        }
        // If the floor printer failed and we have a fallback device, try it.
        if (this.device) {
            return super.printHtml(el, options);
        }
        throw {
            title: result.message?.title || "Error",
            body: result.message?.body,
            canRetry: result.canRetry,
            errorCode: result.errorCode,
        };
    },
});
