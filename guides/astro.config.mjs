// north's guides: how to use north, one task per page. They are published beside the
// landing page (site/index.html) at https://kushagrabainsla.github.io/north/guides/.
// scripts/build_site.sh builds them into the same folder as the landing page.
import sitemap from '@astrojs/sitemap';
import starlight from '@astrojs/starlight';
import { defineConfig } from 'astro/config';
import starlightLinksValidator from 'starlight-links-validator';

const site = 'https://kushagrabainsla.github.io';
const base = '/north';

export default defineConfig({
	site,
	base,
	integrations: [
		starlight({
			title: 'north',
			description: 'How to use north, an open-source assistant that runs on your computer.',
			// The icons come from web/public; scripts/build_site.sh puts them at the site root.
			favicon: '/favicon.svg',
			social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/Kushagrabainsla/north' }],
			editLink: { baseUrl: 'https://github.com/Kushagrabainsla/north/edit/main/guides/' },
			customCss: ['./src/styles/north.css'],
			components: {
				ThemeProvider: './src/components/ThemeProvider.astro',
				ThemeSelect: './src/components/ThemeSelect.astro',
			},
			sidebar: [{ label: 'Start here', items: [{ label: 'All guides', slug: 'guides' }, 'guides/quickstart'] }],
			// A broken link inside the guides fails the build.
			plugins: [starlightLinksValidator()],
		}),
		// Starlight lists only its own pages; this also lists the landing page.
		sitemap({ customPages: [`${site}${base}/`] }),
	],
});
