@extends('layouts.app')

@section('content')
    <h1>Posts</h1>
    @foreach ($posts as $post)
        <article>
            <h2>{{ $post->title }}</h2>
            <p>By {{ $post->author->name }} · {{ $post->comments->count() }} comments</p>
            <ul>
                @foreach ($post->tags as $tag)
                    <li>{{ $tag->name }}</li>
                @endforeach
            </ul>
        </article>
    @endforeach

    {{ $posts->links() }}
@endsection
