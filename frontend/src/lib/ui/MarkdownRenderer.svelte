<script lang="ts">
	/**
	 * MarkdownRenderer — reusable, XSS-safe Markdown renderer for Noosphere Chat.
	 *
	 * Layer: shared UI primitive (`frontend/src/lib/ui/MarkdownRenderer.svelte`).
	 * Strictly escapes all raw HTML (`&`, `<`, `>`, `"`, `'`) BEFORE applying
	 * structural Markdown formatting so untrusted/hostile stream content can never
	 * inject DOM elements or event handlers (`chat.spec.ts` hostile stream rule).
	 *
	 * Supported Markdown constructs:
	 *  - Fenced code blocks (` ```lang ... ``` `)
	 *  - Headings (`#`, `##`, `###`, `####`)
	 *  - Blockquotes (`> ...`)
	 *  - Unordered (`- `, `* `) and ordered (`1. `) lists
	 *  - Markdown tables (`| col | col |`)
	 *  - Horizontal rules (`---`, `***`)
	 *  - Inline code (`` `...` ``), bold (`**...**`), italic (`*...*`), and
	 *    safe `http(s)://` links (`[text](url)`).
	 */

	type Props = {
		/** Raw Markdown text to render. */
		content: string;
		/** Optional extra CSS class on the root container. */
		class?: string;
		/** Optional `data-testid` on the root container. */
		testId?: string;
	};

	let { content, class: className = '', testId }: Props = $props();

	function escapeHtml(raw: string): string {
		return raw
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;')
			.replace(/'/g, '&#39;');
	}

	function formatInline(escapedLine: string): string {
		// 1. Extract inline code spans first so inner asterisks/brackets aren't touched.
		const codeSpans: string[] = [];
		let working = escapedLine.replace(/`([^`\n]+)`/g, (_, code: string) => {
			const idx = codeSpans.length;
			codeSpans.push(`<code class="mai-md__inline-code">${code}</code>`);
			return `\u0000CODE${idx}\u0000`;
		});

		// 2. Safe http/https links: [label](https://...)
		working = working.replace(
			/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
			'<a class="mai-md__link" href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
		);

		// 3. Bold (**text** or __text__)
		working = working.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
		working = working.replace(/__([^_]+)__/g, '<strong>$1</strong>');

		// 4. Italic (*text* or _text_)
		working = working.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');

		// 5. Restore inline code spans
		working = working.replace(/\u0000CODE(\d+)\u0000/g, (_, idxStr: string) => {
			return codeSpans[Number(idxStr)] ?? '';
		});

		return working;
	}

	function parseMarkdownToHtml(source: string): string {
		if (!source) return '';
		const normalized = source.replace(/\r\n/g, '\n');
		const lines = normalized.split('\n');
		const out: string[] = [];
		let i = 0;

		while (i < lines.length) {
			const line = lines[i] ?? '';

			// Fenced code block: ```lang
			const fenceMatch = line.match(/^\s*```([a-zA-Z0-9_-]*)\s*$/);
			if (fenceMatch) {
				const lang = escapeHtml(fenceMatch[1] || 'text');
				const codeLines: string[] = [];
				i += 1;
				while (i < lines.length && !/^\s*```\s*$/.test(lines[i] ?? '')) {
					codeLines.push(escapeHtml(lines[i] ?? ''));
					i += 1;
				}
				if (i < lines.length) i += 1; // skip closing ```
				out.push(
					`<div class="mai-md__code-wrap"><div class="mai-md__code-head">${lang}</div><pre class="mai-md__pre"><code>${codeLines.join('\n')}</code></pre></div>`
				);
				continue;
			}

			// Blank line
			if (line.trim() === '') {
				i += 1;
				continue;
			}

			// Horizontal rule
			if (/^\s*(?:---+|===+|\*\*\*+)\s*$/.test(line)) {
				out.push('<hr class="mai-md__hr" />');
				i += 1;
				continue;
			}

			// Heading: # .. ####
			const headingMatch = line.match(/^\s*(#{1,4})\s+(.+?)\s*$/);
			if (headingMatch) {
				const level = Math.min(4, headingMatch[1].length);
				const tag = level === 1 ? 'h3' : level === 2 ? 'h4' : 'h5';
				const text = formatInline(escapeHtml(headingMatch[2]));
				out.push(`<${tag} class="mai-md__heading mai-md__heading--h${level}">${text}</${tag}>`);
				i += 1;
				continue;
			}

			// Blockquote: > ...
			if (/^\s*>\s?/.test(line)) {
				const quoteLines: string[] = [];
				while (i < lines.length && /^\s*>\s?/.test(lines[i] ?? '')) {
					const stripped = (lines[i] ?? '').replace(/^\s*>\s?/, '');
					quoteLines.push(formatInline(escapeHtml(stripped)));
					i += 1;
				}
				out.push(
					`<blockquote class="mai-md__quote">${quoteLines.map((q) => `<div class="mai-md__quote-line"><span class="mai-md__quote-mark">&gt;</span><span>${q}</span></div>`).join('')}</blockquote>`
				);
				continue;
			}

			// Markdown Table: | ... | followed by | --- |
			if (
				line.includes('|') &&
				i + 1 < lines.length &&
				/^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1] ?? '')
			) {
				const parseCells = (rowText: string): string[] =>
					rowText
						.trim()
						.replace(/^\|/, '')
						.replace(/\|$/, '')
						.split('|')
						.map((c) => formatInline(escapeHtml(c.trim())));
				const headers = parseCells(line);
				i += 2; // skip header + separator
				const bodyRows: string[][] = [];
				while (i < lines.length && (lines[i] ?? '').includes('|') && (lines[i] ?? '').trim() !== '') {
					bodyRows.push(parseCells(lines[i] ?? ''));
					i += 1;
				}
				const thead = `<thead><tr>${headers.map((h) => `<th>${h}</th>`).join('')}</tr></thead>`;
				const tbody = `<tbody>${bodyRows
					.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`)
					.join('')}</tbody>`;
				out.push(`<div class="mai-md__table-wrap"><table class="mai-md__table">${thead}${tbody}</table></div>`);
				continue;
			}

			// Unordered list: - or *
			if (/^\s*[-*]\s+/.test(line)) {
				const items: string[] = [];
				while (i < lines.length && /^\s*[-*]\s+/.test(lines[i] ?? '')) {
					const itemText = (lines[i] ?? '').replace(/^\s*[-*]\s+/, '');
					items.push(`<li>${formatInline(escapeHtml(itemText))}</li>`);
					i += 1;
				}
				out.push(`<ul class="mai-md__list mai-md__list--ul">${items.join('')}</ul>`);
				continue;
			}

			// Ordered list: 1.
			if (/^\s*\d+\.\s+/.test(line)) {
				const items: string[] = [];
				while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i] ?? '')) {
					const itemText = (lines[i] ?? '').replace(/^\s*\d+\.\s+/, '');
					items.push(`<li>${formatInline(escapeHtml(itemText))}</li>`);
					i += 1;
				}
				out.push(`<ol class="mai-md__list mai-md__list--ol">${items.join('')}</ol>`);
				continue;
			}

			// Paragraph: consecutive regular lines
			const paraLines: string[] = [];
			while (
				i < lines.length &&
				(lines[i] ?? '').trim() !== '' &&
				!/^\s*```/.test(lines[i] ?? '') &&
				!/^\s*#{1,4}\s+/.test(lines[i] ?? '') &&
				!/^\s*>\s?/.test(lines[i] ?? '') &&
				!/^\s*[-*]\s+/.test(lines[i] ?? '') &&
				!/^\s*\d+\.\s+/.test(lines[i] ?? '') &&
				!/^\s*(?:---+|===+|\*\*\*+)\s*$/.test(lines[i] ?? '')
			) {
				paraLines.push(formatInline(escapeHtml(lines[i] ?? '')));
				i += 1;
			}
			out.push(`<p class="mai-md__p">${paraLines.join('<br />')}</p>`);
		}

		return out.join('');
	}

	const renderedHtml = $derived(parseMarkdownToHtml(content));
</script>

<div class="mai-md {className}" data-testid={testId}>
	<!-- eslint-disable-next-line svelte/no-at-html-tags -->
	{@html renderedHtml}
</div>

<style>
	.mai-md {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		font-size: var(--mai-text-sm);
		line-height: 1.55;
		color: var(--mai-color-text);
		min-width: 0;
		max-width: 100%;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.mai-md :global(.mai-md__p) {
		margin: 0;
		min-width: 0;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.mai-md :global(.mai-md__heading) {
		margin: var(--mai-space-1) 0 0;
		font-family: var(--mai-font-display);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--mai-color-brass);
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
		padding-bottom: 2px;
	}

	.mai-md :global(.mai-md__heading--h1),
	.mai-md :global(.mai-md__heading--h2) {
		font-size: var(--mai-text-base);
	}

	.mai-md :global(.mai-md__heading--h3),
	.mai-md :global(.mai-md__heading--h4) {
		font-size: var(--mai-text-sm);
	}

	.mai-md :global(.mai-md__list) {
		margin: 0;
		padding-left: var(--mai-space-5);
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.mai-md :global(.mai-md__list--ul) {
		list-style-type: square;
	}

	.mai-md :global(.mai-md__list--ol) {
		list-style-type: decimal;
	}

	.mai-md :global(.mai-md__inline-code) {
		padding: 1px 5px;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-brass);
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		word-break: break-word;
		box-decoration-break: clone;
		-webkit-box-decoration-break: clone;
	}

	.mai-md :global(.mai-md__code-wrap) {
		min-width: 0;
		max-width: 100%;
		box-sizing: border-box;
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left: var(--mai-chip-accent-width, 3px) solid var(--mai-color-brass-dim);
		border-radius: var(--mai-radius-sm);
		background: var(--mai-color-bg);
		overflow: hidden;
	}

	.mai-md :global(.mai-md__code-head) {
		padding: 2px var(--mai-space-2);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--mai-color-text-muted);
		background: var(--mai-color-surface);
		border-bottom: var(--mai-border-width) solid var(--mai-color-border);
	}

	.mai-md :global(.mai-md__pre) {
		margin: 0;
		padding: var(--mai-space-2) var(--mai-space-3);
		min-width: 0;
		max-width: 100%;
		box-sizing: border-box;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		line-height: 1.5;
		color: var(--mai-color-text);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		word-break: break-word;
		overflow-x: hidden;
	}

	.mai-md :global(.mai-md__pre code) {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.mai-md :global(.mai-md__quote) {
		margin: 0;
		padding: var(--mai-space-2) var(--mai-space-3);
		min-width: 0;
		max-width: 100%;
		box-sizing: border-box;
		border-left: var(--mai-chip-accent-width, 3px) solid var(--mai-color-brass-dim);
		background: var(--mai-color-surface);
		color: var(--mai-color-text-muted);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		display: flex;
		flex-direction: column;
		gap: 2px;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.mai-md :global(.mai-md__quote-line) {
		display: flex;
		gap: var(--mai-space-2);
		min-width: 0;
	}

	.mai-md :global(.mai-md__quote-mark) {
		color: var(--mai-color-brass);
		user-select: none;
		flex: 0 0 auto;
	}

	.mai-md :global(.mai-md__table-wrap) {
		min-width: 0;
		max-width: 100%;
		box-sizing: border-box;
		overflow-x: auto;
	}

	.mai-md :global(.mai-md__table) {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--mai-text-xs);
	}

	.mai-md :global(.mai-md__table th),
	.mai-md :global(.mai-md__table td) {
		padding: var(--mai-space-1) var(--mai-space-2);
		border: var(--mai-border-width) solid var(--mai-color-border);
		text-align: left;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.mai-md :global(.mai-md__table th) {
		background: var(--mai-color-surface);
		color: var(--mai-color-brass);
		font-family: var(--mai-font-mono);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.mai-md :global(.mai-md__hr) {
		border: none;
		border-top: var(--mai-border-width) solid var(--mai-color-border);
		margin: var(--mai-space-1) 0;
	}

	.mai-md :global(.mai-md__link) {
		color: var(--mai-color-brass);
		text-decoration: underline;
	}
</style>
