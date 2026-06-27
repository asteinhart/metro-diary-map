<script>
	import { base } from '$app/paths';
	import '../global.css';

	let { children } = $props();
</script>

<svelte:head>
	<link rel="icon" href="{base}/thumbs.webp" />
	<link rel="apple-touch-icon" href="{base}/thumbs.webp" />
</svelte:head>

{@render children()}
