import re

with open(r"C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py", 'r', encoding='utf-8') as f:
    content = f.read()

# Find and replace the NIM result section
old_pattern = r'("drawing_count": len\(nim\.drawings\),\n    \})'
new_text = '"drawing_count": len(nim.drawings),\n        "job_id": spec.project_id,\n    }'

content = re.sub(old_pattern, new_text, content)

with open(r"C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py", 'w', encoding='utf-8') as f:
    f.write(content)

print("Done!")
