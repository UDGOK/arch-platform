-- Run this in Supabase SQL Editor: https://supabase.com/dashboard/project/iryrqahvpeqjvludqopn/sql

CREATE TABLE IF NOT EXISTS arch_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_name TEXT,
    job_data JSONB NOT NULL,
    status TEXT DEFAULT 'completed',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Enable RLS
ALTER TABLE arch_jobs ENABLE ROW LEVEL SECURITY;

-- Allow anon key to read/write
CREATE POLICY "Allow all" ON arch_jobs FOR ALL USING (true) WITH CHECK (true);
