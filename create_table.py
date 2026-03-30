#!/usr/bin/env python3
"""Create arch_jobs table in Supabase"""
import psycopg2

conn = psycopg2.connect(
    host='db.iryrqahvpeqjvludqopn.supabase.co',
    port='6543',
    database='postgres',
    user='postgres',
    password='yqITFrDnV4vmXaF3',
    sslmode='require'
)
cur = conn.cursor()

# Create table
cur.execute('''
CREATE TABLE IF NOT EXISTS arch_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_name TEXT,
    job_data JSONB NOT NULL,
    status TEXT DEFAULT 'completed',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
''')
conn.commit()
print('Table created successfully')

# List tables
cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
tables = cur.fetchall()
print('Tables:', [t[0] for t in tables])

cur.close()
conn.close()
