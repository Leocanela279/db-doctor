import { prisma } from '@/lib/prisma';

export async function GET(req: Request) {
  const q = new URL(req.url).searchParams.get('q') ?? '';
  const rows = await prisma.$queryRawUnsafe(`SELECT * FROM posts WHERE title = '${q}'`);
  const safe = await prisma.$queryRaw`SELECT id, title FROM posts WHERE title = ${q}`;
  return Response.json({ rows, safe });
}
