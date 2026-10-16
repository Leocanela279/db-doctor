<?php

namespace App\Http\Controllers;

use App\Http\Resources\PostResource;
use App\Models\Comment;
use App\Models\Post;
use App\Models\User;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

class PostController extends Controller
{
    // GET /posts  -> "la página tarda 4 segundos"
    public function index(Request $request)
    {
        $posts = Post::where('status', 'published')
            ->orderByDesc('published_at')
            ->paginate(20);

        return view('posts.index', compact('posts'));
    }

    // GET /dashboard
    public function dashboard()
    {
        $users = User::all();
        $stats = [];

        foreach ($users as $user) {
            $stats[] = [
                'name' => $user->name,
                'posts' => $user->posts()->count(),
                'last_comment' => Comment::where('user_id', $user->id)->latest()->first(),
            ];
        }

        $todayPosts = Post::whereDate('created_at', now()->toDateString())->get()->count();

        return view('dashboard', compact('stats', 'todayPosts'));
    }

    // GET /api/posts/search?q=
    public function search(Request $request)
    {
        $q = $request->input('q');

        $posts = Post::where('title', 'like', "%{$q}%")
            ->orWhereRaw("lower(body) like '%$q%'")
            ->get();

        return PostResource::collection($posts);
    }

    // GET /api/posts/{slug}
    public function show(string $slug)
    {
        $post = Post::with(['author', 'tags'])->where('slug', $slug)->firstOrFail();

        return new PostResource($post);
    }

    // Cron: every minute
    public function publishScheduled()
    {
        $posts = Post::where('status', 'scheduled')
            ->where('published_at', '<=', now())
            ->get();

        foreach ($posts as $post) {
            $post->status = 'published';
            $post->save();
        }
    }

    public function hasComments(Post $post): bool
    {
        return Comment::where('post_id', $post->id)->count() > 0;
    }

    public function export()
    {
        return DB::select('SELECT * FROM posts WHERE deleted_at IS NULL ORDER BY created_at DESC');
    }
}
