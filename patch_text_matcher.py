"""Patch text_matcher.py to handle po_per_page list format."""

filepath = r"qwen_backend\text_matcher.py"

with open(filepath, "rb") as f:
    data = f.read()

# Find the exact byte pattern: "missed = []\r\n\r\n    # " (the Header fields comment line)
anchor = b"missed = []\r\n\r\n    # "
idx = data.find(anchor)

if idx == -1:
    if b"isinstance(extraction_result, list)" in data:
        print("Already patched!")
    else:
        print("ERROR: Could not find anchor pattern")
    exit()

# We insert right after "missed = []\r\n"
insert_point = idx + len(b"missed = []\r\n")

patch = (
    b"\r\n"
    b"    # Handle po_per_page format where extraction_result is a list of dicts.\r\n"
    b"    # Merge into a single dict: header from first entry, line_items concatenated.\r\n"
    b"    if isinstance(extraction_result, list):\r\n"
    b"        merged = {}\r\n"
    b"        all_items = []\r\n"
    b"        for entry in extraction_result:\r\n"
    b"            if not isinstance(entry, dict):\r\n"
    b"                continue\r\n"
    b'            for k, v in entry.items():\r\n'
    b'                if k == "line_items":\r\n'
    b"                    if isinstance(v, list):\r\n"
    b"                        all_items.extend(v)\r\n"
    b'                elif k not in merged and k not in ("_page", "_total_pages", "_error"):\r\n'
    b"                    merged[k] = v\r\n"
    b'        merged["line_items"] = all_items\r\n'
    b"        extraction_result = merged\r\n"
)

new_data = data[:insert_point] + patch + data[insert_point:]

with open(filepath, "wb") as f:
    f.write(new_data)

print("PATCHED successfully!")
