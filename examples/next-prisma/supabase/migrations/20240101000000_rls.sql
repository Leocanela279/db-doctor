alter table comments enable row level security;

create policy "Users read own comments"
  on comments for select
  using (auth.uid() = user_id);

create policy "Users update own comments"
  on comments for update
  using ((select auth.uid()) = user_id);
