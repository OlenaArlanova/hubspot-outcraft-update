import os
import traceback

from flask import Flask, jsonify, request

import pipeline

app = Flask(__name__)

TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN")


@app.route("/")
def health():
    return jsonify({"ok": True, "service": "hubspot-sync-pipeline"})


@app.route("/run", methods=["POST"])
def run():
    if not TRIGGER_TOKEN or request.headers.get("X-Trigger-Token") != TRIGGER_TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    try:
        outcraft_rows = pipeline.run_outcraft()
        hubspot_rows  = pipeline.run_hubspot()
        all_rows      = outcraft_rows + hubspot_rows

        pipeline.save_csv(all_rows)
        pipeline.save_sheet(all_rows)

        return jsonify({
            "ok": True,
            "outcraft_rows": len(outcraft_rows),
            "hubspot_rows": len(hubspot_rows),
            "total_rows": len(all_rows),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
