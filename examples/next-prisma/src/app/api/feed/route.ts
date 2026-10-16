import { prisma } from '@/lib/prisma';

export async function GET() {
  const users = await prisma.user.findMany();

  const feed = await Promise.all(
    users.map(async (u) => {
      const latest = await prisma.post.findFirst({
        where: { authorId: u.id },
        orderBy: { createdAt: 'desc' },
      });
      return { user: u.name, latest };
    }),
  );

  const total = (await prisma.post.findMany({ where: { published: true } })).length;

  return Response.json({ feed, total });
}
