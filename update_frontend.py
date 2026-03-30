#!/usr/bin/env python3
"""Update frontend to use job_id-based exports"""
import re

with open('public/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Update dlPDF to use job_id-based export
old_pdf = '''async function dlPDF() {
  if (!lastJob) { toast('Generate drawings first','err'); return; }
  toast('Generating PDF...','info');
  var name = (lastJob.project_name||'project').replace(/\\s+/g,'_');
  try {
    var jobData = cleanJob(lastJob);
    var jsonStr = JSON.stringify({job: jobData});
    if (jsonStr.length > 4500000) {
      toast('Job data too large - try with smaller project','err'); return;
    }
    var b = await postBinary('/api/export/pdf', {job: jobData});
    dlBlob(b, name+'_construction.pdf');
    toast('PDF downloaded','ok');
  } catch(e) { toast('PDF failed: ' + e.message,'err'); log('PDF: '+e.message,'e'); }
}'''

new_pdf = '''async function dlPDF() {
  if (!lastJob) { toast('Generate drawings first','err'); return; }
  toast('Generating PDF...','info');
  var name = (lastJob.project_name||'project').replace(/\\s+/g,'_');
  var jobId = lastJob.job_id || lastJob.id;
  try {
    // Try job_id-based export first (more reliable with Supabase)
    var b = await postBinary('/api/export/' + encodeURIComponent(jobId) + '/pdf');
    dlBlob(b, name+'_construction.pdf');
    toast('PDF downloaded','ok');
  } catch(e) { 
    // Fallback to body-based export
    try {
      var jobData = cleanJob(lastJob);
      var b = await postBinary('/api/export/pdf', {job: jobData});
      dlBlob(b, name+'_construction.pdf');
      toast('PDF downloaded','ok');
    } catch(e2) { 
      toast('PDF failed: ' + e2.message,'err'); 
      log('PDF: '+e2.message,'e'); 
    }
  }
}'''

content = content.replace(old_pdf, new_pdf)

# Update dlPackage to use job_id-based export
old_pkg = '''async function dlPackage() {
  if (!lastJob) { toast('Generate drawings first','err'); return; }
  toast('Building package...','info');
  var name = (lastJob.project_name||'project').replace(/\\s+/g,'_');
  try {
    var jobData = cleanJob(lastJob);
    var jsonStr = JSON.stringify({job: jobData});
    if (jsonStr.length > 4500000) {
      toast('Job data too large - try with smaller project','err'); return;
    }
    var b = await postBinary('/api/export/package', {job: jobData});
    dlBlob(b, name+'_construction_set.zip');
    toast('Package downloaded','ok');
  } catch(e) { toast('Package failed: ' + e.message,'err'); log('Package: '+e.message,'e'); }
}'''

new_pkg = '''async function dlPackage() {
  if (!lastJob) { toast('Generate drawings first','err'); return; }
  toast('Building package...','info');
  var name = (lastJob.project_name||'project').replace(/\\s+/g,'_');
  var jobId = lastJob.job_id || lastJob.id;
  try {
    // Try job_id-based export first (more reliable with Supabase)
    var b = await postBinary('/api/export/' + encodeURIComponent(jobId) + '/package');
    dlBlob(b, name+'_construction_set.zip');
    toast('Package downloaded','ok');
  } catch(e) { 
    // Fallback to body-based export
    try {
      var jobData = cleanJob(lastJob);
      var b = await postBinary('/api/export/package', {job: jobData});
      dlBlob(b, name+'_construction_set.zip');
      toast('Package downloaded','ok');
    } catch(e2) { 
      toast('Package failed: ' + e2.message,'err'); 
      log('Package: '+e2.message,'e'); 
    }
  }
}'''

content = content.replace(old_pkg, new_pkg)

with open('public/index.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("Frontend updated successfully!")
print("Changes made:")
print("  1. Updated dlPDF to use job_id-based export with fallback")
print("  2. Updated dlPackage to use job_id-based export with fallback")
