-- Server-managed public property assets. The API writes with the service-role key.
insert into storage.buckets (id, name, public)
values
  ('property-images', 'property-images', true),
  ('property-documents', 'property-documents', true)
on conflict (id) do update set public = excluded.public;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'storage' and tablename = 'objects'
      and policyname = 'Public read property assets'
  ) then
    create policy "Public read property assets"
    on storage.objects for select
    using (bucket_id in ('property-images', 'property-documents'));
  end if;
end $$;
