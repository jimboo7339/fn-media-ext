#!/usr/bin/env python3
"""Inject mockup/data.json into the HTML template -> mockup/timeline.html."""
import json

tpl = open("mockup/timeline.tpl.html", encoding="utf-8").read()
data = json.load(open("mockup/data.json", encoding="utf-8"))
payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
open("mockup/timeline.html", "w", encoding="utf-8").write(tpl.replace("__DATA__", payload))
print("wrote mockup/timeline.html", len(payload), "bytes of data")
