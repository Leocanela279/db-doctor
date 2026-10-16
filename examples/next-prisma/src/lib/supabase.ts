import { createClient } from '@supabase/supabase-js';

export const supabase = createClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!);

export async function commentsForPosts(ids: number[]) {
  const result: Record<number, unknown[]> = {};
  for (const id of ids) {
    const { data } = await supabase.from('comments').select('*').eq('post_id', id);
    result[id] = data ?? [];
  }
  return result;
}

export async function searchPosts(term: string) {
  const { data } = await supabase
    .from('posts')
    .select('id, title, author:users(name)')
    .ilike('title', `%${term}%`)
    .order('created_at', { ascending: false });
  return data;
}
