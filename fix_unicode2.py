import re

with open('arch-platform/api/server.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Pattern 1: Fix project_name in export_pdf
# Find and replace the project sanitization in export_pdf function
pattern1 = r'(project = req\.job\.get\("project_name","project"\)\.replace\(" ","_"\)(\[:40\])?)'
replacement1 = r"raw_name = req.job.get('project_name','project')\n    project = ''.join(c if ord(c) < 128 else '_' for c in raw_name).replace(' ','_')[:40]"

content = re.sub(pattern1, replacement1, content)
print("Applied pattern 1 fix")

# Pattern 2: Fix project_name in job_id exports (export_pdf_by_id, export_package_by_id)
pattern2 = r'(project = job\.get\("project_name","project"\)\.replace\(" ","_"\)(\[:40\])?)'
replacement2 = r"raw_name = job.get('project_name','project')\n    project = ''.join(c if ord(c) < 128 else '_' for c in raw_name).replace(' ','_')[:40]"

content = re.sub(pattern2, replacement2, content)
print("Applied pattern 2 fix")

# Pattern 3: Fix fname in DXF export
pattern3 = r'fname = sheet_names\[idx\]'
replacement3 = "fname = sheet_names[idx]\n        fname = ''.join(c if ord(c) < 128 else '_' for c in fname)"

content = re.sub(pattern3, replacement3, content)
print("Applied pattern 3 fix")

# Also fix package export (uses req.job.get)
pattern4 = r'(project = req\.job\.get\("project_name","project"\)\.replace\(" ","_"\)(\[:40\])?)'
# Already handled by pattern1, but check for remaining ones
matches = re.findall(pattern4, content)
if matches:
    print(f"Warning: {len(matches)} patterns still found")

with open('arch-platform/api/server.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Done!")
