-- Create the property-images storage bucket
-- Run in Supabase SQL Editor OR use the dashboard:
--   Storage → New Bucket → Name: "property-images" → Public: ON

-- Note: Supabase Storage buckets cannot be created via SQL directly.
-- Use this SQL to set up RLS policies AFTER creating the bucket in the dashboard.

-- Allow public read access to property images
create policy "Public read property images"
on storage.objects for select
using (bucket_id = 'property-images');

-- Allow authenticated users (brokers) to upload
create policy "Brokers can upload property images"
on storage.objects for insert
with check (bucket_id = 'property-images');

-- Allow authenticated users to delete their uploads
create policy "Brokers can delete property images"
on storage.objects for delete
using (bucket_id = 'property-images');
