import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			// Static export for GitHub Pages. `fallback` serves the SPA shell for any
			// path GitHub Pages can't match. See https://svelte.dev/docs/kit/adapter-static#GitHub-Pages
			adapter: adapter({
				fallback: '404.html'
			}),

			// On GitHub Pages the site is served from /<repo>/, so everything is prefixed
			// with that base. CI sets BASE_PATH to '/metro-diary-map'; `dev` and local
			// builds fall back to '' (served from the root).
			paths: {
				base: process.argv.includes('dev') ? '' : (process.env.BASE_PATH ?? '')
			}
		})
	]
});
