#!/usr/bin/env python3
"""
Minimal Odoo IoT Box — ESC/POS bridge for generic network printers.
Implements the Odoo hw_proxy HTTP API so Odoo POS can print receipts
and kitchen tickets to standard TCP:9100 ESC/POS printers.

Configuration is managed via the web UI at http://iot-box:8069/
and stored in /app/config/config.json (persisted via Docker volume).
"""
import os
import io
import json
import base64
import logging
import socket
import concurrent.futures
import xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image
from flask import Flask, request, jsonify, render_template_string

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)


CONFIG_FILE = Path(os.getenv("IOT_CONFIG_DIR", "/app/config")) / "config.json"
CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Config helpers ────────────────────────────────────────────────────────────

IMAGE_MODES = ("graphics", "bitImageRaster", "bitImageColumn")
DEFAULT_IMAGE_MODE = "graphics"   # works on modern Epson; Munbyn/generic = bitImageRaster


def _default_config():
    return {
        "receipt_printer_ip":  os.getenv("RECEIPT_PRINTER_IP", ""),
        "kitchen_printer_ip":  os.getenv("KITCHEN_PRINTER_IP", ""),
        "printer_port":        int(os.getenv("PRINTER_PORT", "9100")),
        "printer_timeout":     int(os.getenv("PRINTER_TIMEOUT", "5")),
        # Last scanned subnet remembered between page loads.
        "scan_subnet":         os.getenv("LAN_SUBNET", "192.168.1").rstrip("."),
        # Image mode for the default receipt + kitchen printers.
        "receipt_image_mode":  DEFAULT_IMAGE_MODE,
        "kitchen_image_mode":  DEFAULT_IMAGE_MODE,
        # list of {"floor": "<floor name>", "ip": "<printer ip>", "image_mode": "..."}.
        # When a receipt print request includes a `floor`, we route to the
        # matching printer; otherwise fall back to receipt_printer_ip.
        "floor_printers":      [],
    }


def _normalize_mode(mode):
    mode = (mode or "").strip()
    return mode if mode in IMAGE_MODES else DEFAULT_IMAGE_MODE


def load_config():
    cfg = _default_config()
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text())
            cfg.update(stored)
        except Exception:
            pass
    # Ensure keys exist for older configs
    cfg.setdefault("floor_printers", [])
    cfg.setdefault("scan_subnet", detect_subnet())
    cfg.setdefault("receipt_image_mode", DEFAULT_IMAGE_MODE)
    cfg.setdefault("kitchen_image_mode", DEFAULT_IMAGE_MODE)
    # Every floor printer needs a mode; default any missing
    for fp in cfg["floor_printers"]:
        fp.setdefault("image_mode", DEFAULT_IMAGE_MODE)
    return cfg

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

# ── Printer helpers ───────────────────────────────────────────────────────────

def get_printer(ip, cfg):
    from escpos.printer import Network
    return Network(ip, port=cfg["printer_port"], timeout=cfg["printer_timeout"])

def printer_reachable(ip, port, timeout=3):
    if not ip:
        return False
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def scan_network_printers(subnet, port=9100, timeout=0.5):
    """Scan subnet (e.g. '192.168.50') for devices with port 9100 open."""
    def check(host):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                try:
                    name = socket.gethostbyaddr(host)[0]
                except Exception:
                    name = ""
                return {"ip": host, "port": port, "name": name}
        except Exception:
            return None

    hosts = [f"{subnet}.{i}" for i in range(1, 255)]
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
        for result in ex.map(check, hosts):
            if result:
                found.append(result)
    found.sort(key=lambda x: int(x["ip"].split(".")[-1]))
    return found


def detect_subnet():
    """Return LAN subnet from env var (set in docker-compose) or fall back to 192.168.1."""
    subnet = os.getenv("LAN_SUBNET", "").strip()
    if subnet:
        return subnet.rstrip(".")
    return "192.168.1"

def xml_to_escpos(printer, xml_str):
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        printer.text(xml_str)
        printer.cut()
        return

    def process(node):
        tag  = (node.tag or "").lower()
        text = (node.text or "").strip()

        if tag == "receipt":
            for child in node: process(child)

        elif tag in ("line", "br"):
            left = right = ""
            for child in node:
                ctag = (child.tag or "").lower()
                if ctag == "left":   left  = (child.text or "").strip()
                elif ctag == "right": right = (child.text or "").strip()
                elif ctag == "value": left  = (child.text or "").strip()
            if right:
                printer.text(f"{left:<24}{right:>8}\n")
            elif left:
                printer.text(f"{left}\n")
            else:
                printer.text("\n")

        elif tag == "div":
            printer.text("-" * 32 + "\n")

        elif tag in ("h1", "h2", "title"):
            printer.set(align="center", bold=True, double_height=(tag == "h1"))
            if text: printer.text(text + "\n")
            for child in node: process(child)
            printer.set(align="left", bold=False, double_height=False)

        elif tag == "center":
            printer.set(align="center")
            if text: printer.text(text + "\n")
            for child in node: process(child)
            printer.set(align="left")

        elif tag == "b":
            printer.set(bold=True)
            if text: printer.text(text)
            for child in node: process(child)
            printer.set(bold=False)

        elif tag == "barcode":
            try:
                printer.barcode((node.text or "").strip(), node.get("encoding", "EAN13").upper())
            except Exception as e:
                log.warning(f"Barcode error: {e}")
        else:
            if text: printer.text(text + "\n")
            for child in node: process(child)

    process(root)
    printer.cut()

# ── Web config UI ─────────────────────────────────────────────────────────────

CONFIG_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IoT Box — Printer Config</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #f4f6f9; display: flex; justify-content: center;
           padding: 40px 16px; }
    .card { background: white; border-radius: 12px; padding: 32px;
            width: 100%; max-width: 520px;
            box-shadow: 0 2px 12px rgba(0,0,0,.08); }
    h1 { font-size: 20px; color: #1a1a2e; margin-bottom: 6px; }
    .subtitle { color: #666; font-size: 14px; margin-bottom: 28px; }
    label { display: block; font-size: 13px; font-weight: 600;
            color: #444; margin-bottom: 6px; margin-top: 20px; }
    input[type=text], input[type=number] {
            width: 100%; padding: 10px 14px; border: 1px solid #ddd;
            border-radius: 8px; font-size: 15px; outline: none;
            transition: border .2s; }
    input:focus { border-color: #875a7b; }
    .status { display: inline-block; width: 10px; height: 10px;
              border-radius: 50%; margin-right: 6px; }
    .status.ok  { background: #28a745; }
    .status.err { background: #dc3545; }
    .status-row { font-size: 13px; color: #555; margin-top: 6px;
                  display: flex; align-items: center; }
    .btn { margin-top: 16px; width: 100%; padding: 12px;
           background: #875a7b; color: white; border: none;
           border-radius: 8px; font-size: 15px; font-weight: 600;
           cursor: pointer; transition: background .2s; }
    .btn:hover { background: #6d4a65; }
    .btn-scan { background: #6c757d; }
    .btn-scan:hover { background: #545b62; }
    .btn-test { background: #17a2b8; }
    .btn-test:hover { background: #138496; }
    .alert { padding: 10px 14px; border-radius: 8px; margin-top: 16px;
             font-size: 14px; display: none; }
    .alert.success { background: #d4edda; color: #155724; display: block; }
    .alert.error   { background: #f8d7da; color: #721c24; display: block; }
    .divider { border: none; border-top: 1px solid #eee; margin: 24px 0; }
    .scanner-results { margin-top: 16px; }
    .printer-row { display: flex; align-items: center; justify-content: space-between;
                   padding: 10px 12px; border: 1px solid #eee; border-radius: 8px;
                   margin-bottom: 8px; background: #fafafa; }
    .printer-info { font-size: 13px; }
    .printer-ip { font-weight: 600; color: #333; }
    .printer-name { color: #888; font-size: 12px; }
    .assign-btns { display: flex; gap: 6px; }
    .btn-assign { padding: 5px 10px; font-size: 12px; border: none;
                  border-radius: 6px; cursor: pointer; font-weight: 600; }
    .btn-receipt { background: #875a7b; color: white; }
    .btn-kitchen { background: #28a745; color: white; }
    .scanning-msg { text-align: center; color: #666; font-size: 14px;
                    padding: 16px; display: none; }
    .hint { font-size: 12px; color: #888; margin-top: 4px; }
    .assign-target {
      padding: 5px 8px; border: 1px solid #ccc; border-radius: 6px;
      font-size: 12px; background: #fff;
    }
    .floor-row {
      display: grid;
      grid-template-columns: 1fr 1fr 130px auto auto auto;
      gap: 6px;
      align-items: center;
      margin-bottom: 6px;
      padding: 8px;
      background: #fafafa;
      border-radius: 8px;
    }
    .floor-row input,
    .floor-row select {
      padding: 8px 10px;
      border: 1px solid #ddd;
      border-radius: 6px;
      font-size: 13px;
      margin: 0;
      background: #fff;
    }
    .ip-with-mode {
      display: flex; gap: 6px; align-items: center;
    }
    .ip-with-mode input { flex: 1; }
    .mode-select {
      padding: 8px 10px;
      border: 1px solid #ddd;
      border-radius: 6px;
      font-size: 12px;
      background: #fff;
      min-width: 130px;
    }
    .floor-status { display: inline-block; width: 10px; height: 10px;
                    border-radius: 50%; }
    .btn-row {
      border: none; border-radius: 6px; padding: 6px 10px;
      cursor: pointer; font-size: 12px; font-weight: 600;
    }
    .btn-row-test { background: #17a2b8; color: #fff; }
    .btn-row-del  { background: #dc3545; color: #fff; padding: 6px 10px; }
  </style>
</head>
<body>
<div class="card">
  <h1>🖨 IoT Box</h1>
  <p class="subtitle">ESC/POS Printer Configuration</p>

  {% if message %}
  <div class="alert {{ 'success' if success else 'error' }}">{{ message }}</div>
  {% endif %}

  <!-- Scanner -->
  <label>Scan Network for Printers</label>
  <div style="display:flex; gap:8px; margin-top:6px;">
    <input type="text" id="subnet" value="{{ cfg.scan_subnet }}" placeholder="192.168.50" style="flex:1;">
    <button type="button" class="btn btn-scan" style="margin:0;width:auto;padding:10px 18px;"
            onclick="scanNetwork()">Scan</button>
  </div>
  <p class="hint">Edit and click Scan. The subnet is saved with the configuration so this persists.</p>
  <p class="scanning-msg" id="scanning-msg">⏳ Scanning network... (up to 15 seconds)</p>
  <div class="scanner-results" id="scan-results"></div>

  <hr class="divider">

  <!-- Manual config -->
  <form method="POST" action="/config" id="config-form">
    <!-- Mirror of the scan input so it persists across reloads -->
    <input type="hidden" name="scan_subnet" id="scan_subnet_hidden" value="{{ cfg.scan_subnet }}">

    <label>Kitchen Printer IP</label>
    <div class="ip-with-mode">
      <input type="text" name="kitchen_printer_ip" id="kitchen_ip"
             value="{{ cfg.kitchen_printer_ip }}" placeholder="e.g. 192.168.50.99">
      <select name="kitchen_image_mode" class="mode-select" title="Image mode">
        <option value="graphics" {{ 'selected' if cfg.kitchen_image_mode == 'graphics' else '' }}>graphics (modern Epson)</option>
        <option value="bitImageRaster" {{ 'selected' if cfg.kitchen_image_mode == 'bitImageRaster' else '' }}>bitImageRaster (Munbyn/generic)</option>
        <option value="bitImageColumn" {{ 'selected' if cfg.kitchen_image_mode == 'bitImageColumn' else '' }}>bitImageColumn (legacy Epson)</option>
      </select>
    </div>
    <div class="status-row">
      <span class="status {{ 'ok' if kitchen_ok else 'err' }}"></span>
      {{ 'Reachable' if kitchen_ok else 'Not reachable' }}
    </div>

    <label>Default Receipt Printer IP <span class="hint">(used when no floor matches)</span></label>
    <div class="ip-with-mode">
      <input type="text" name="receipt_printer_ip" id="receipt_ip"
             value="{{ cfg.receipt_printer_ip }}" placeholder="e.g. 192.168.50.10">
      <select name="receipt_image_mode" class="mode-select" title="Image mode">
        <option value="graphics" {{ 'selected' if cfg.receipt_image_mode == 'graphics' else '' }}>graphics (modern Epson)</option>
        <option value="bitImageRaster" {{ 'selected' if cfg.receipt_image_mode == 'bitImageRaster' else '' }}>bitImageRaster (Munbyn/generic)</option>
        <option value="bitImageColumn" {{ 'selected' if cfg.receipt_image_mode == 'bitImageColumn' else '' }}>bitImageColumn (legacy Epson)</option>
      </select>
    </div>
    <div class="status-row">
      <span class="status {{ 'ok' if receipt_ok else 'err' }}"></span>
      {{ 'Reachable' if receipt_ok else 'Not reachable' }}
    </div>

    <hr class="divider">

    <label>Floor → Receipt Printer</label>
    <p class="hint">
      Each restaurant floor can have its own receipt printer. Floor names must
      match the names defined in Odoo (POS → Restaurant → Floors).
    </p>
    <div id="floor-rows">
      {% for fp in cfg.floor_printers %}
      <div class="floor-row">
        <input type="text" name="floor_name[]" placeholder="Floor name (e.g. Main Floor)"
               value="{{ fp.floor }}">
        <input type="text" name="floor_ip[]" placeholder="Printer IP"
               value="{{ fp.ip }}">
        <select name="floor_image_mode[]" class="mode-select">
          <option value="graphics" {{ 'selected' if fp.image_mode == 'graphics' else '' }}>graphics</option>
          <option value="bitImageRaster" {{ 'selected' if fp.image_mode == 'bitImageRaster' else '' }}>bitImageRaster</option>
          <option value="bitImageColumn" {{ 'selected' if fp.image_mode == 'bitImageColumn' else '' }}>bitImageColumn</option>
        </select>
        <span class="floor-status status {{ 'ok' if fp.get('reachable') else 'err' }}"></span>
        <button type="button" class="btn-row btn-row-test"
                onclick="testRow(this)">Test</button>
        <button type="button" class="btn-row btn-row-del"
                onclick="this.parentElement.remove()">×</button>
      </div>
      {% endfor %}
    </div>
    <button type="button" class="btn btn-scan" style="margin-top:8px;"
            onclick="addFloorRow()">+ Add Floor Mapping</button>

    <label>Printer Port</label>
    <input type="number" name="printer_port" value="{{ cfg.printer_port }}" placeholder="9100">

    <button class="btn" type="submit" style="margin-top:18px;">Save Configuration</button>
  </form>

  <form method="POST" action="/test_print" style="margin-top:8px;">
    <button class="btn btn-test" type="submit">Test Default Receipt Printer</button>
  </form>
</div>

<script>
// Keep hidden scan_subnet field in sync with the visible subnet input
document.getElementById('subnet').addEventListener('input', (e) => {
  document.getElementById('scan_subnet_hidden').value = e.target.value.trim();
});

function getFloorOptions() {
  const rows = document.querySelectorAll('#floor-rows .floor-row');
  return Array.from(rows).map((r, idx) => {
    const name = r.querySelector('input[name="floor_name[]"]').value || `Floor #${idx+1}`;
    return `<option value="floor:${idx}">${name}</option>`;
  }).join('');
}

function scanNetwork() {
  const subnet = document.getElementById('subnet').value.trim();
  // Persist on the hidden field so a future Save keeps it
  document.getElementById('scan_subnet_hidden').value = subnet;
  document.getElementById('scanning-msg').style.display = 'block';
  document.getElementById('scan-results').innerHTML = '';
  fetch('/scan?subnet=' + encodeURIComponent(subnet))
    .then(r => r.json())
    .then(data => {
      document.getElementById('scanning-msg').style.display = 'none';
      const el = document.getElementById('scan-results');
      if (!data.length) {
        el.innerHTML = '<p style="color:#888;font-size:13px;margin-top:8px;">No printers found on ' + subnet + '.0/24</p>';
        return;
      }
      el.innerHTML = data.map(p => `
        <div class="printer-row">
          <div class="printer-info">
            <div class="printer-ip">${p.ip}</div>
            <div class="printer-name">${p.name || 'Generic ESC/POS Printer'} &nbsp;·&nbsp; port ${p.port}</div>
          </div>
          <div class="assign-btns">
            <select class="assign-target" data-ip="${p.ip}" onchange="assignToTarget(this)">
              <option value="">Assign to…</option>
              <option value="kitchen">Kitchen</option>
              <option value="receipt">Default Receipt</option>
              <option value="__newfloor__">+ New Floor</option>
              ${getFloorOptions()}
            </select>
          </div>
        </div>`).join('');
    })
    .catch(() => {
      document.getElementById('scanning-msg').style.display = 'none';
      document.getElementById('scan-results').innerHTML = '<p style="color:red;font-size:13px;">Scan failed.</p>';
    });
}

function assignToTarget(sel) {
  const ip = sel.dataset.ip;
  const val = sel.value;
  if (!val || !ip) { sel.value = ''; return; }
  if (val === 'kitchen') {
    document.getElementById('kitchen_ip').value = ip;
  } else if (val === 'receipt') {
    document.getElementById('receipt_ip').value = ip;
  } else if (val === '__newfloor__') {
    const name = prompt('Floor name (must match Odoo floor name):');
    if (!name) { sel.value = ''; return; }
    addFloorRow(name, ip);
  } else if (val.startsWith('floor:')) {
    const idx = parseInt(val.slice(6), 10);
    const row = document.querySelectorAll('#floor-rows .floor-row')[idx];
    if (row) row.querySelector('input[name="floor_ip[]"]').value = ip;
  }
  sel.value = '';
}

function addFloorRow(name = '', ip = '') {
  const wrap = document.getElementById('floor-rows');
  const row = document.createElement('div');
  row.className = 'floor-row';
  row.innerHTML = `
    <input type="text" name="floor_name[]" placeholder="Floor name (e.g. Main Floor)" value="${name}">
    <input type="text" name="floor_ip[]" placeholder="Printer IP" value="${ip}">
    <select name="floor_image_mode[]" class="mode-select">
      <option value="graphics" selected>graphics</option>
      <option value="bitImageRaster">bitImageRaster</option>
      <option value="bitImageColumn">bitImageColumn</option>
    </select>
    <span class="floor-status status err"></span>
    <button type="button" class="btn-row btn-row-test" onclick="testRow(this)">Test</button>
    <button type="button" class="btn-row btn-row-del" onclick="this.parentElement.remove()">×</button>`;
  wrap.appendChild(row);
}

function testRow(btn) {
  const row  = btn.parentElement;
  const name = row.querySelector('input[name="floor_name[]"]').value || 'Floor';
  const ip   = row.querySelector('input[name="floor_ip[]"]').value;
  const mode = row.querySelector('select[name="floor_image_mode[]"]').value || 'graphics';
  if (!ip) { alert('Enter a printer IP first.'); return; }
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = '/test_print';
  form.innerHTML = `<input type="hidden" name="ip" value="${ip}">
                    <input type="hidden" name="label" value="${name}">
                    <input type="hidden" name="image_mode" value="${mode}">`;
  document.body.appendChild(form);
  form.submit();
}
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def config_page():
    return _render_config(load_config())


@app.route("/scan")
def scan():
    subnet  = request.args.get("subnet", detect_subnet()).strip()
    port    = load_config().get("printer_port", 9100)
    log.info(f"Scanning {subnet}.0/24 for port {port}...")
    results = scan_network_printers(subnet, port=port)
    log.info(f"Found {len(results)} printer(s)")
    return jsonify(results)

@app.route("/config", methods=["POST"])
def save_config_route():
    cfg = load_config()
    cfg["receipt_printer_ip"]  = request.form.get("receipt_printer_ip", "").strip()
    cfg["kitchen_printer_ip"]  = request.form.get("kitchen_printer_ip", "").strip()
    cfg["receipt_image_mode"]  = _normalize_mode(request.form.get("receipt_image_mode"))
    cfg["kitchen_image_mode"]  = _normalize_mode(request.form.get("kitchen_image_mode"))
    cfg["scan_subnet"]         = (request.form.get("scan_subnet", "") or detect_subnet()).strip().rstrip(".")
    cfg["printer_port"]        = int(request.form.get("printer_port", 9100) or 9100)
    # Floor printers come as parallel lists: floor_name[] / floor_ip[] / floor_image_mode[]
    names = request.form.getlist("floor_name[]")
    ips   = request.form.getlist("floor_ip[]")
    modes = request.form.getlist("floor_image_mode[]")
    floor_printers = []
    for i, (name, ip) in enumerate(zip(names, ips)):
        name = (name or "").strip()
        ip   = (ip or "").strip()
        mode = _normalize_mode(modes[i] if i < len(modes) else DEFAULT_IMAGE_MODE)
        if name and ip:
            floor_printers.append({"floor": name, "ip": ip, "image_mode": mode})
    cfg["floor_printers"] = floor_printers
    save_config(cfg)
    log.info(f"Config saved: receipt={cfg['receipt_printer_ip']} ({cfg['receipt_image_mode']}) "
             f"kitchen={cfg['kitchen_printer_ip']} ({cfg['kitchen_image_mode']}) "
             f"floors={len(floor_printers)}")
    return _render_config(cfg, "Configuration saved.", success=True)


def _render_config(cfg, message=None, success=False):
    receipt_ok = printer_reachable(cfg["receipt_printer_ip"], cfg["printer_port"])
    kitchen_ok = printer_reachable(cfg["kitchen_printer_ip"], cfg["printer_port"])
    for fp in cfg.get("floor_printers", []):
        fp["reachable"] = printer_reachable(fp.get("ip"), cfg["printer_port"])
    return render_template_string(CONFIG_HTML, cfg=cfg,
                                  receipt_ok=receipt_ok, kitchen_ok=kitchen_ok,
                                  subnet=detect_subnet(),
                                  message=message, success=success)

@app.route("/test_print", methods=["POST"])
def test_print_route():
    """Test print for an arbitrary IP + image mode. Defaults to the receipt printer."""
    cfg   = load_config()
    ip    = (request.form.get("ip") or cfg["receipt_printer_ip"]).strip()
    label = request.form.get("label") or "Receipt Printer"
    mode  = _normalize_mode(request.form.get("image_mode") or cfg.get("receipt_image_mode"))
    if not ip:
        return _render_config(cfg, "No printer IP provided.", success=False)
    try:
        printer = get_printer(ip, cfg)
        # Text portion — should always work
        printer.set(align="center", bold=True)
        printer.text("=== TEST PRINT ===\n")
        printer.set(align="left", bold=False)
        printer.text(f"{label}: {ip}:{cfg['printer_port']}\n")
        printer.text(f"Image mode: {mode}\n")
        # Image portion — proves the chosen image mode is supported
        try:
            from PIL import Image, ImageDraw
            img = Image.new("L", (300, 60), 255)
            ImageDraw.Draw(img).text((10, 18), "IMAGE TEST OK", fill=0)
            printer.image(img, impl=mode, center=True)
        except Exception as ie:
            printer.text(f"(image test failed: {ie})\n")
        printer.cut()
        try:
            printer.close()
        except Exception:
            pass
        msg, ok = f"Test print sent to {label} ({ip}) using {mode}.", True
    except Exception as e:
        msg, ok = f"Print failed: {e}", False
    return _render_config(cfg, msg, success=ok)

# ── Odoo hw_proxy API ─────────────────────────────────────────────────────────

@app.route("/hw_proxy/hello")
def hello():
    return "ping"

@app.route("/hw_proxy/status_json", methods=["GET", "POST", "OPTIONS"])
def status():
    cfg    = load_config()
    port   = cfg["printer_port"]
    data   = request.get_json(silent=True) or {}
    req_id = data.get("id", 1)
    ok     = printer_reachable(cfg["receipt_printer_ip"], port) if cfg["receipt_printer_ip"] else False
    # Odoo's rpc() extracts response.result as `drivers` — must be JSON-RPC format
    return jsonify({
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "scanner": None,
            "scale":   None,
            "printer": {"status": "connected" if ok else "disconnected"}
        }
    })

@app.route("/hw_proxy/print_xml_receipt", methods=["POST", "GET"])
def print_receipt():
    cfg = load_config()
    if not cfg["receipt_printer_ip"]:
        return jsonify({"status": "error", "message": "Receipt printer IP not configured"}), 500
    try:
        data = request.get_json(silent=True) or {}
        log.info(f"Receipt print → {cfg['receipt_printer_ip']}")
        xml_to_escpos(get_printer(cfg["receipt_printer_ip"], cfg), data.get("receipt", ""))
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error(f"Receipt print error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/hw_proxy/print_xml_order", methods=["POST", "GET"])
def print_order():
    cfg       = load_config()
    target_ip = cfg["kitchen_printer_ip"] or cfg["receipt_printer_ip"]
    if not target_ip:
        return jsonify({"status": "error", "message": "No printer configured"}), 500
    try:
        data = request.get_json(silent=True) or {}
        log.info(f"Kitchen print → {target_ip}")
        xml_to_escpos(get_printer(target_ip, cfg),
                      data.get("order", data.get("receipt", "")))
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error(f"Kitchen print error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/hw_proxy/handshake", methods=["GET", "POST", "OPTIONS"])
def handshake():
    data  = request.get_json(silent=True) or {}
    return jsonify({"id": data.get("id", 1), "jsonrpc": "2.0", "result": {"status": "connected"}})


def _resolve_floor_printer(cfg, floor_name):
    """Find the configured (ip, image_mode) for the given floor name.
    Returns (None, None) if no match."""
    if not floor_name:
        return (None, None)
    target = floor_name.strip().lower()
    for fp in cfg.get("floor_printers", []):
        if (fp.get("floor") or "").strip().lower() == target:
            return (fp.get("ip") or None, _normalize_mode(fp.get("image_mode")))
    return (None, None)


@app.route("/hw_proxy/default_printer_action", methods=["GET", "POST", "OPTIONS"])
def default_printer_action():
    cfg      = load_config()
    data     = request.get_json(silent=True) or {}
    req_id   = data.get("id", 1)
    params   = data.get("params", {})
    pdata    = params.get("data", {})
    img_b64  = pdata.get("receipt", "")
    floor    = (pdata.get("floor") or "").strip()

    if not img_b64:
        return jsonify({"id": req_id, "jsonrpc": "2.0",
                        "result": {"status": "error", "message": "No receipt data"}})

    caller     = request.remote_addr or ""
    user_agent = request.headers.get("User-Agent", "").lower()
    is_server  = "python" in user_agent or "odoo" in user_agent

    if is_server:
        printer_type = "KITCHEN"
        target_ip = cfg["kitchen_printer_ip"] or cfg["receipt_printer_ip"]
        image_mode = _normalize_mode(cfg.get("kitchen_image_mode"))
        log_extra = ""
    else:
        printer_type = "RECEIPT"
        floor_ip, floor_mode = _resolve_floor_printer(cfg, floor)
        if floor_ip:
            target_ip = floor_ip
            image_mode = floor_mode
            log_extra = f" (floor: {floor})"
        else:
            target_ip = cfg["receipt_printer_ip"]
            image_mode = _normalize_mode(cfg.get("receipt_image_mode"))
            log_extra = f" (floor: {floor or 'unspecified'} → fallback)"

    log.info(f"[{printer_type}] Print request from {caller} (ua: {user_agent[:40]}) → "
             f"{target_ip} mode={image_mode}{log_extra}")

    printer = None
    try:
        img = Image.open(io.BytesIO(base64.b64decode(img_b64)))
        printer = get_printer(target_ip, cfg)
        printer.image(img, impl=image_mode, center=True)
        printer.cut()
        log.info(f"[{printer_type}] Print OK")
        return jsonify({"id": req_id, "jsonrpc": "2.0", "result": {"status": "ok"}})
    except Exception as e:
        log.error(f"[{printer_type}] Print error: {e}")
        return jsonify({"id": req_id, "jsonrpc": "2.0",
                        "result": {"status": "error", "message": str(e)}})
    finally:
        if printer:
            try:
                printer.close()
            except Exception:
                pass


@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def catch_all(path):
    body = request.get_data(as_text=True)[:500]
    log.info(f"UNKNOWN REQUEST: {request.method} /{path} | body: {body}")
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8069"))
    cfg = load_config()
    log.info(f"IoT Box starting — Receipt: {cfg['receipt_printer_ip']}, Kitchen: {cfg['kitchen_printer_ip']}")
    log.info(f"Config UI available at http://0.0.0.0:{port}/")
    app.run(host="0.0.0.0", port=port, debug=False)
