<script module lang="ts">
	/**
	 * HistoryTags — compact, honest tag preview for history rows (W7A).
	 *
	 * Layer: shared UI primitive. Owns nothing: no window geometry, no server
	 * scoping, no global state, no module-level mutable singletons. All state
	 * is instance-local Svelte 5 runes; safe to render on the server.
	 *
	 * Wire contract (history-tags W6B schema, verbatim):
	 *   - `tag_preview` — EntityTagPreviewView: first ≤3 tag names plus the
	 *     honest total attached to the entity.
	 *   - `firstPage` — optional already-fetched first TagPage of the full list.
	 *   - `loadMore` — optional async adapter panels supply later; it returns
	 *     one bounded TagPage starting at `offset`. Every fetch is bounded by
	 *     PAGE_LIMIT (AGENTS.md §1 pagination invariant); rows are appended
	 *     deduplicated by `tag_id`, and `has_more` drives the Load more control.
	 *
 * Interaction contract (simplified event model — no pointer-coordinate
 * state, no capture listeners):
 *   - the mount harness parks the mouse off-widget before goto, so
 *     Chromium's post-navigation pointer replay cannot hover-open a
 *     trigger; the component tracks no coordinates itself.
 *   - a real mouse pointermove over the trigger opens it; a click-toggle
 *     close suppresses hover until the pointer leaves the trigger.
 *   - click/tap toggles from the open-state at activation start
 *     (pointerdown/keydown snapshot): a tap's own focus-open is not
 *     undone by that same tap's click, while a click on an already-open
 *     popup (hover/focus-opened) closes it.
 *   - the last-opened popup stacks above earlier sibling popups so
 *     overlapping hover-kept-open popups never block each other's
 *     controls; the seed is a per-instance wall-clock sample, not a
 *     module-level counter.
 *   - click/tap toggles; keyboard focus opens; blur outside closes
 *   - Escape closes and restores focus to the trigger
 *   - pointerdown outside the root closes
 *   - aria-expanded / aria-controls / aria-haspopup="dialog" wired
	 *
	 * Honesty contract (C08 rule 6): loading, error, empty, and the
	 * "Showing X of Y" truncation count are always surfaced; rows render only
	 * as they are loaded — never unbounded, never eager.
	 */
	import type { components } from '../api/schema.generated';

	type EntityTagPreviewView = components['schemas']['EntityTagPreviewView'];
	type TagPage = components['schemas']['TagPage'];
	type TagView = components['schemas']['TagView'];

	/** Panels supply this adapter: one bounded page of the entity's tag list. */
	export type TagPageLoader = (offset: number, limit: number) => Promise<TagPage>;
</script>

<script lang="ts">
	import Chip from './Chip.svelte';

	type Props = {
		/** Bounded preview plus honest total from the wire row. */
		tag_preview: EntityTagPreviewView;
		/** Optional already-fetched first page of the full tag list. */
		firstPage?: TagPage | null;
		/** Optional async adapter returning a bounded page starting at `offset`. */
		loadMore?: TagPageLoader | null;
		/** Whose tags these are; used for the popup's accessible label. */
		label?: string;
		/** data-testid prefix for internal elements. */
		testId?: string;
	};

	/** Bounded page size for the full tag list (AGENTS.md §1; drawer-sized). */
	const PAGE_LIMIT = 10;
	/** Popup width bound and the viewport margin it must respect. */
	const POP_MAX_WIDTH = 320;
	const VIEWPORT_MARGIN = 8;

	let {
		tag_preview,
		firstPage = null,
		loadMore = null,
		label = 'Tags',
		testId = 'history-tags'
	}: Props = $props();

	/** Stable unique id per Svelte 5 instance — SSR-safe, no Math.random. */
	const uid = $props.id();
	const triggerId = `mai-history-tags-trigger-${uid}`;
	const popId = `mai-history-tags-pop-${uid}`;

	let root = $state<HTMLElement | null>(null);
	/** The popup dialog itself; its rendered height feeds the flip clamp. */
	let popEl = $state<HTMLElement | null>(null);
	let open = $state(false);
	/** Viewport-clamped fixed placement for the popup (updated in syncFlip). */
	let popTop = $state(0);
	let popLeft = $state(0);
	let items = $state<TagView[]>([]);
	let total = $state<number | null>(null);
	let hasMore = $state(false);
	let nextOffset = $state(0);
	let started = $state(false);
	let loading = $state(false);
	let loadingMore = $state(false);
	let error = $state(false);
	/**
	 * Wall-clock sample taken at open time: the last-opened popup stacks
	 * above earlier sibling popups (30 + zSeed % 100000), so overlapping
	 * hover-kept-open popups never intercept each other's controls.
	 * Per-instance state only — no module-level mutable singleton.
	 */
	let zSeed = $state(0);
	/**
	 * Open-state snapshot taken when an activation begins (trigger
	 * pointerdown/keydown). The click toggles only if the state still
	 * matches the snapshot: a tap focuses the trigger first, whose
	 * focus-open must not be undone by that same tap's click.
	 */
	let toggleBase = $state(false);

	/** True when more tags exist on the wire than the preview shows. */
	const extraCount = $derived(Math.max(0, tag_preview.total - tag_preview.preview.length));
	const canLoadMore = $derived(open && hasMore && !error && loadMore !== null);

	/**
	 * Merge one wire page into the deduplicated row list. Offset accounting
	 * stays in wire space (offset + served rows) so overlapping pages collapse
	 * by `tag_id` without skipping server rows.
	 */
	function adoptPage(page: TagPage) {
		const seen = new Set(items.map((tag) => tag.tag_id));
		for (const tag of page.items ?? []) {
			if (!seen.has(tag.tag_id)) {
				seen.add(tag.tag_id);
				items.push(tag);
			}
		}
		total = page.total;
		hasMore = page.has_more;
		nextOffset = page.offset + (page.items?.length ?? 0);
	}

	async function requestPage(offset: number, isMore: boolean) {
		const loader = loadMore;
		if (!loader) return;
		if (isMore) loadingMore = true;
		else loading = true;
		error = false;
		try {
			adoptPage(await loader(offset, PAGE_LIMIT));
		} catch {
			error = true;
		} finally {
			if (isMore) loadingMore = false;
			else loading = false;
		}
	}

	/** Lazily adopt the supplied first page or fetch page one; runs once per mount. */
	function ensureLoaded() {
		if (started || loading || loadingMore) return;
		started = true;
		if (firstPage) {
			adoptPage(firstPage);
		} else if (loadMore) {
			void requestPage(0, false);
		}
		// No loaders: honest preview-only mode; the popup says so.
	}

	/**
	 * Viewport-clamped fixed placement. The popup is `position: fixed`, so
	 * opening it inside a scrolled list (`.mai-chat-drawer__list`,
	 * `overflow-y: auto`) or a clipped accordion (`.mai-accordion`,
	 * `overflow: hidden`) never creates scrollbars on that parent and never
	 * gets clipped by it. Horizontally `left` clamps to
	 * [VIEWPORT_MARGIN, innerWidth − width − VIEWPORT_MARGIN]; vertically it
	 * opens below the trigger when that fits inside
	 * innerHeight − VIEWPORT_MARGIN, above when only above fits, and clamps
	 * inside the viewport when neither side fits.
	 */
	function syncFlip() {
		if (!root || typeof window === 'undefined') return;
		const rect = root.getBoundingClientRect();
		const available = Math.max(0, window.innerWidth - VIEWPORT_MARGIN * 2);
		const width = Math.min(POP_MAX_WIDTH, available);
		popLeft = Math.min(
			Math.max(rect.left, VIEWPORT_MARGIN),
			Math.max(VIEWPORT_MARGIN, window.innerWidth - width - VIEWPORT_MARGIN)
		);
		const popHeight = popEl?.offsetHeight ?? 0;
		const gap = 4; // --mai-space-1
		const below = rect.bottom + gap;
		const maxTop = Math.max(VIEWPORT_MARGIN, window.innerHeight - VIEWPORT_MARGIN - popHeight);
		const above = rect.top - gap - popHeight;
		popTop =
			below + popHeight <= window.innerHeight - VIEWPORT_MARGIN || above < VIEWPORT_MARGIN
				? Math.min(below, maxTop)
				: above;
	}

	function openPopover() {
		if (open) return;
		open = true;
		zSeed = Date.now();
		ensureLoaded();
		syncFlip();
	}

	// The popup's real height is only measurable once it renders (its
	// `hidden` display flips to flex); re-measure after the open flush and
	// whenever the loaded row count changes that height.
	$effect(() => {
		if (!open) return;
		void items.length;
		syncFlip();
	});

	let suppressFocusOpen = false;

	function closePopover(refocus = false) {
		if (!open) return;
		open = false;
		if (refocus) {
			const trigger = document.getElementById(triggerId);
			if (trigger && document.activeElement !== trigger) {
				suppressFocusOpen = true;
				trigger.focus();
			}
		}
	}

	function handleTriggerFocus() {
		if (suppressFocusOpen) {
			suppressFocusOpen = false;
			return;
		}
		openPopover();
	}

	/**
	 * Mouse hover-open, single handler: a real mouse pointermove over the
	 * trigger opens it unless a click-toggle close suppressed hover. The
	 * suppression lifts on pointerleave, so reopening requires leave + move
	 * — the genuine re-entry the interaction contract demands. Touch moves
	 * never hover (their taps open through the click toggle).
	 */
	let suppressHover = $state(false);

	function handleTriggerPointerMove(e: PointerEvent) {
		if (e.pointerType === 'mouse' && !suppressHover) openPopover();
	}

	function handleTriggerPointerLeave() {
		suppressHover = false;
	}

	/** Activation begins: snapshot the open-state the click will toggle from. */
	function handleTriggerPointerDown() {
		toggleBase = open;
	}

	/** Keyboard activation (Enter/Space) snapshots the same way. */
	function handleTriggerKeydown() {
		toggleBase = open;
	}

	function handleTriggerClick() {
		if (open !== toggleBase) {
			// State changed mid-activation (a tap's own focus-open): the
			// toggle already happened — do not undo it.
			toggleBase = open;
			return;
		}
		if (open) {
			closePopover();
			suppressHover = true;
		} else {
			openPopover();
		}
	}

	function handleRootKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			e.stopPropagation();
			closePopover(true);
		}
	}

	function handleRootFocusOut(e: FocusEvent) {
		if (!open || !root) return;
		const next = e.relatedTarget;
		if (next instanceof Node && !root.contains(next)) closePopover();
	}

	function handleWindowPointerDown(e: PointerEvent) {
		if (!open || !root) return;
		const target = e.target;
		if (target instanceof Node && !root.contains(target)) closePopover();
	}

	function handleWindowResize() {
		if (open) syncFlip();
	}

	/** Any scroll anywhere (capture phase): a fixed popup must follow its
	 * trigger's viewport position while open, so re-clamp on every scroll. */
	function handleWindowScroll() {
		if (open) syncFlip();
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions:
  role="group" correctly identifies a disclosure/popover widget group. The
  onkeydown handler catches Escape bubbled from the popup (e.g. its Load more
  button) and the onfocusout boundary closes on honest blur; this is the
  intended a11y pattern for a popover. -->
<!-- svelte-ignore a11y_no_static_element_interactions:
  Same justification as above — group is the correct role for this widget. -->
<div
	bind:this={root}
	role="group"
	aria-label={label}
	class="mai-history-tags"
	data-testid={testId}
	onkeydown={handleRootKeydown}
	onfocusout={handleRootFocusOut}
>
	<button
		id={triggerId}
		type="button"
		class="mai-history-tags__trigger"
		data-testid={`${testId}-trigger`}
		aria-haspopup="dialog"
		aria-expanded={open}
		aria-controls={popId}
		onpointerdown={handleTriggerPointerDown}
		onpointermove={handleTriggerPointerMove}
		onpointerleave={handleTriggerPointerLeave}
		onfocus={handleTriggerFocus}
		onkeydown={handleTriggerKeydown}
		onclick={handleTriggerClick}
	>
		{#each tag_preview.preview as name (name)}
			<Chip variant="neutral" class="mai-history-tags__chip" title={name} data-testid={`${testId}-chip`}>
				{name}
			</Chip>
		{/each}
		{#if tag_preview.preview.length === 0}
			<span class="mai-history-tags__none">No tags</span>
		{/if}
		{#if extraCount > 0}
			<span
				class="mai-history-tags__more"
				data-testid={`${testId}-more`}
				title={`${extraCount} more tag${extraCount === 1 ? '' : 's'}`}
			>
				+{extraCount}
			</span>
		{/if}
	</button>

	<div
		id={popId}
		bind:this={popEl}
		role="dialog"
		class="mai-history-tags__pop"
		style:top="{popTop}px"
		style:left="{popLeft}px"
		style:z-index={open ? 30 + (zSeed % 100_000) : undefined}
		aria-label={`${label} — all tags`}
		aria-busy={loading || loadingMore}
		data-testid={`${testId}-pop`}
		hidden={!open}
	>
		{#if loading}
			<p class="mai-history-tags__state" data-testid={`${testId}-loading`}>Loading tags…</p>
		{:else if error}
			<p
				class="mai-history-tags__state mai-history-tags__state--error"
				role="alert"
				data-testid={`${testId}-error`}
			>
				Tag list failed to load.
			</p>
			<button
				type="button"
				class="mai-history-tags__action"
				data-testid={`${testId}-retry`}
				onclick={() => {
					error = false;
					if (items.length === 0) void requestPage(0, false);
					else void requestPage(nextOffset, true);
				}}
			>
				Try again
			</button>
		{:else if items.length === 0}
			<p class="mai-history-tags__state" data-testid={`${testId}-empty`}>
				{total === null && !firstPage && !loadMore ? 'Full tag list unavailable.' : 'No tags attached.'}
			</p>
		{:else}
			<ul class="mai-history-tags__list" data-testid={`${testId}-list`}>
				{#each items as tag (tag.tag_id)}
					<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
					<!-- Rows are intentionally keyboard-walkable (W7A contract): each
					     list item is a focus stop so keyboard users can traverse the
					     bounded tag list; the Chip carries the visible pill. -->
					<li
						class="mai-history-tags__row"
						data-testid={`${testId}-row`}
						tabindex="0"
						title={tag.name}
					>
						<Chip variant="neutral" class="mai-history-tags__chip">
							{tag.name}
						</Chip>
					</li>
				{/each}
			</ul>
		{/if}
		{#if total !== null}
			<p class="mai-history-tags__count" data-testid={`${testId}-count`}>
				Showing {items.length} of {total}
			</p>
		{/if}
		{#if canLoadMore}
			<button
				type="button"
				class="mai-history-tags__action"
				data-testid={`${testId}-load-more`}
				disabled={loadingMore}
				onclick={() => void requestPage(nextOffset, true)}
			>
				{loadingMore ? 'Loading…' : 'Load more'}
			</button>
		{/if}
	</div>
</div>

<svelte:window
	onpointerdown={handleWindowPointerDown}
	onresize={handleWindowResize}
	onscrollcapture={handleWindowScroll}
/>

<style>
	.mai-history-tags {
		position: relative;
		display: inline-flex;
		max-width: 100%;
		font-family: var(--mai-font-sans, inherit);
	}

	/* ---- Trigger ---- */
	.mai-history-tags__trigger {
		display: inline-flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-1, 4px);
		min-width: 0;
		max-width: 100%;
		overflow: hidden;
		min-height: var(--mai-target-min, 32px);
		padding: var(--mai-space-1, 4px) var(--mai-space-2, 8px);
		background: transparent;
		border: none;
		border-radius: var(--mai-radius-md, 2px);
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm, 0.875rem);
		cursor: pointer;
	}

	.mai-history-tags__trigger:hover {
		background: var(--mai-color-surface-raised);
	}

	.mai-history-tags__trigger:focus-visible {
		outline: var(--mai-focus-ring-width, 2px) solid var(--mai-focus-ring-color);
		outline-offset: var(--mai-focus-ring-offset, 2px);
	}

	/* Compact/truncated preview chips (Chip renders its own content span).
	 * The hard 9rem cap keeps the popup's intrinsic (max-content) width
	 * small; `flex: 0 1 auto` + `min-width: 0` still shrink a chip below it
	 * when its flex line is narrower — narrow containers never overflow. */
	.mai-history-tags :global(.mai-history-tags__chip) {
		flex: 0 1 auto;
		min-width: 0;
		max-width: 9rem;
	}

	.mai-history-tags :global(.mai-history-tags__chip .mai-chip__content) {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.mai-history-tags__more {
		flex-shrink: 0;
		padding: 0 var(--mai-space-1, 4px);
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm, 0.875rem);
		font-weight: var(--mai-weight-medium, 500);
		white-space: nowrap;
	}

	/* ---- Popup dialog ---- */
	/* position: fixed + inline top/left (viewport-clamped in syncFlip) keeps
	 * the popup out of every ancestor scroll/clip context. */
	.mai-history-tags__pop {
		position: fixed;
		top: 0;
		left: 0;
		z-index: 30;
		width: max-content;
		max-width: min(320px, calc(100vw - 16px));
		padding: var(--mai-space-2, 8px);
		border: var(--mai-border-width, 1px) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md, 2px);
		background: var(--mai-color-surface);
		color: var(--mai-color-text);
		box-shadow: var(--mai-shadow-pop, 0 4px 16px rgb(0 0 0 / 0.25));
	}

	.mai-history-tags__pop:not([hidden]) {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 8px);
	}

	.mai-history-tags__state {
		margin: 0;
		padding: var(--mai-space-1, 4px) 0;
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm, 0.875rem);
	}

	.mai-history-tags__state--error {
		color: var(--mai-color-danger, var(--mai-color-text));
	}

	/* Bounded scroll region: rows render only as loaded, viewport never unbounded. */
	.mai-history-tags__list {
		margin: 0;
		padding: 0;
		max-height: 16rem;
		overflow-y: auto;
		list-style: none;
	}

	.mai-history-tags__row {
		padding: var(--mai-space-1, 4px) var(--mai-space-2, 8px);
		border-bottom: 1px solid var(--mai-color-border);
		font-size: var(--mai-text-sm, 0.875rem);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.mai-history-tags__row:last-child {
		border-bottom: none;
	}

	.mai-history-tags__none {
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-sm, 0.875rem);
		white-space: nowrap;
	}

	.mai-history-tags__row:focus-visible {
		outline: var(--mai-focus-ring-width, 2px) solid var(--mai-focus-ring-color);
		outline-offset: -2px;
	}

	.mai-history-tags__count {
		margin: 0;
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-history-tags__action {
		align-self: flex-start;
		min-height: var(--mai-target-min, 32px);
		padding: var(--mai-space-1, 4px) var(--mai-space-3, 12px);
		border: var(--mai-border-width, 1px) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md, 2px);
		background: transparent;
		color: var(--mai-color-text);
		font-size: var(--mai-text-sm, 0.875rem);
		cursor: pointer;
	}

	.mai-history-tags__action:hover:not(:disabled) {
		background: var(--mai-color-surface-raised);
		border-color: var(--mai-color-brass-dim, var(--mai-color-border));
	}

	.mai-history-tags__action:focus-visible {
		outline: var(--mai-focus-ring-width, 2px) solid var(--mai-focus-ring-color);
		outline-offset: var(--mai-focus-ring-offset, 2px);
	}

	.mai-history-tags__action:disabled {
		opacity: 0.6;
		cursor: default;
	}
</style>
