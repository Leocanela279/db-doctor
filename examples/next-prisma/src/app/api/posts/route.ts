import { prisma } from '@/lib/prisma';

// GET /api/posts?q=&page=  -> slow
export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const q = searchParams.get('q') ?? '';
  const page = Number(searchParams.get('page') ?? 1);

  const posts = await prisma.post.findMany({
    where: { published: true, title: { contains: q, mode: 'insensitive' } },
    orderBy: { createdAt: 'desc' },
    skip: (page - 1) * 20,
    take: 20,
  });

  const result = [];
  for (const post of posts) {
    const author = await prisma.user.findUnique({ where: { id: post.authorId } });
    const commentsCount = await prisma.comment.count({ where: { postId: post.id } });
    result.push({ ...post, author: author?.name, commentsCount });
  }

  return Response.json(result);
}
