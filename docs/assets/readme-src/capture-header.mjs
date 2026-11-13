#!/usr/bin/env node
/**
 * capture-header.mjs — renders docs/assets/readme-src/header.html to
 * docs/assets/machina-hero-banner.png (headless Chromium, deviceScaleFactor=2).
 *
 * The header is built on the app's own theme (mechanicus.css) + self-hosted
 * fonts, with auto-sized CSS frames. This script additionally ASSERTS that no
 * content overflows the banner plate, so the "drifted frames" defect of the old
 * hand-placed SVG cannot recur: any overflow fails the capture loudly.
 *
 * Run from the repo root:  node docs/assets/readme-src/capture-header.mjs
 * Requires: frontend/node_modules (Playwright + Chromium already installed).
 */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../frontend/package.json')
);
const { chromium } = require('@playwright/test');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, 'header.html');
const OUT = path.resolve(HERE, '../machina-hero-banner.png');

const browser = await chromium.launch({ args: ['--no-sandbox'] });
try {
  const page = await browser.newPage({
    viewport: { width: 1240, height: 380 },
    deviceScaleFactor: 2
  });
  await page.goto('file://' + SRC, { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);

  const hero = page.locator('#hero');
  await hero.waitFor({ state: 'visible' });

  // Overflow guard: every descendant's painted box must stay inside the plate
  // AND clear of the inner sanctum frame (inset 12px), so frames never drift.
  const overflow = await page.evaluate(() => {
    const plate = document.getElementById('hero');
    const pr = plate.getBoundingClientRect();
    const FRAME = 12; // .hero__frame inset
    const bad = [];
    for (const el of plate.querySelectorAll('*')) {
      if (el.closest('.hero__frame') || el.classList.contains('hero__band')) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      const tol = 1.5;
      const over = {
        l: Math.round(pr.left + FRAME - r.left), r: Math.round(r.right - (pr.right - FRAME)),
        t: Math.round(pr.top + FRAME - r.top), b: Math.round(r.bottom - (pr.bottom - FRAME))
      };
      if (over.l > tol || over.r > tol || over.t > tol || over.b > tol) {
        bad.push({ el: el.className?.toString?.().slice(0, 40) || el.tagName, over });
      }
    }
    return { plate: { w: pr.width, h: pr.height }, bad };
  });
  if (overflow.bad.length > 0) {
    console.error('HEADER OVERFLOW DETECTED — frames drifted:', JSON.stringify(overflow, null, 2));
    process.exit(1);
  }
  console.log('header plate', overflow.plate, '— no overflow');

  await hero.screenshot({ path: OUT });
  console.log('wrote', OUT);
} finally {
  await browser.close();
}
