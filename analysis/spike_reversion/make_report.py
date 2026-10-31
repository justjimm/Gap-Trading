"""Inject results/report_data.json into report_template.html -> report.html."""
import json, os
here = os.path.dirname(os.path.abspath(__file__))
data = open(os.path.join(here, "results", "report_data.json")).read()
tpl = open(os.path.join(here, "report_template.html")).read()
open(os.path.join(here, "report.html"), "w").write(tpl.replace("__DATA__", data))
print("wrote report.html", len(tpl) + len(data))
