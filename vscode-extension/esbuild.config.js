const esbuild = require('esbuild');

esbuild.buildSync({
  entryPoints: ['src/extension.ts'],
  bundle: true,
  outfile: 'out/extension.js',
  external: ['vscode'],
  format: 'cjs',
  platform: 'node',
  target: 'node16',
  sourcemap: false,
  minify: false,
});

console.log('Extension bundled successfully into out/extension.js');
