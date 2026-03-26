import urllib.request
import json

req = urllib.request.Request(
    'http://localhost:8000/vendors/TEST001/template',
    data=json.dumps({
        "format_type": "single_po_multipage",
        "header_fields": ["test"],
        "line_item_fields": [],
        "prompt_instructions": None,
        "extraction_rules": []
    }).encode(),
    headers={'Content-Type': 'application/json'}
)
try:
    with urllib.request.urlopen(req) as response:
        print("Success:", response.read().decode())
except Exception as e:
    print("Error:", e)
    if hasattr(e, 'read'):
        print(e.read().decode())
