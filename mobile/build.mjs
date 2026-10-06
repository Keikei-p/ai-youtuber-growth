import { build } from 'esbuild';
import { cp, mkdir, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const source = join(root, 'www');
const out = join(root, 'dist');

await rm(out, { recursive: true, force: true });
await mkdir(out, { recursive: true });
await cp(join(source, 'index.html'), join(out, 'index.html'));
await cp(join(source, 'style.css'), join(out, 'style.css'));

await build({
  entryPoints: [join(source, 'app.js')],
  outfile: join(out, 'app.js'),
  bundle: true,
  minify: true,
  sourcemap: false,
  platform: 'browser',
  format: 'iife',
  target: ['es2022'],
});

console.log('[MOBILE] dist build complete');
