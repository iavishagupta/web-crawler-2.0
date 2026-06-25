import json

def write_json_report(page_data, filename="report.json"):
    with open(filename, "w", encoding="utf-8") as jf:
        json.dump(page_data, jf, indent=2, sort_keys=True)