import re

with open('arch-platform/api/server.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix: project = req.job.get("project_name","project").replace(" ","_")[:40]
old_pattern = 'project = req.job.get("project_name","project").replace(" ","_")[:40]'
new_pattern = '''raw_name = req.job.get("project_name","project")
    project = ''.join(c if ord(c) < 128 else '_' for c in raw_name).replace(" ","_")[:40]'''

if old_pattern in content:
    content = content.replace(old_pattern, new_pattern, 1)
    print('Fixed export_pdf project sanitization')

# Fix: project = job.get("project_name","project").replace(" ","_")[:40]
old_pattern2 = 'project = job.get("project_name","project").replace(" ","_")[:40]'
new_pattern2 = '''raw_name = job.get("project_name","project")
    project = ''.join(c if ord(c) < 128 else '_' for c in raw_name).replace(" ","_")[:40]'''

if old_pattern2 in content:
    content = content.replace(old_pattern2, new_pattern2)
    print('Fixed job_id export project sanitization')

# Fix fname for dxf - sanitize sheet names
old_fname = 'fname = sheet_names[idx]\n        dxf_bytes = sheets[fname]'
new_fname = '''fname = sheet_names[idx]
        # Sanitize for ASCII - latin-1 can't encode special chars
        fname = ''.join(c if ord(c) < 128 else '_' for c in fname)
        dxf_bytes = sheets.get(fname, sheets[sheet_names[idx]])  # fallback'''

if old_fname in content:
    content = content.replace(old_fname, new_fname)
    print('Fixed DXF fname sanitization')

with open('arch-platform/api/server.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Done!')
