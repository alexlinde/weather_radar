const esbuild = require('esbuild');
const fs = require('fs');
const path = require('path');

const watch = process.argv.includes('--watch');
const distDir = path.join(__dirname, 'dist');

async function build() {
  fs.mkdirSync(distDir, { recursive: true });

  const jsResult = await esbuild.build({
    entryPoints: [path.join(__dirname, 'app.js')],
    bundle: true,
    minify: true,
    sourcemap: true,
    format: 'esm',
    outfile: path.join(distDir, 'radar.min.js'),
    metafile: true,
  });

  await esbuild.build({
    entryPoints: [path.join(__dirname, 'style.css')],
    bundle: true,
    minify: true,
    outfile: path.join(distDir, 'style.min.css'),
  });

  let html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  html = html.replace('href="style.css"', 'href="style.min.css"');
  html = html.replace(
    '<script type="module" src="app.js"></script>',
    '<script type="module" src="radar.min.js"></script>',
  );
  fs.writeFileSync(path.join(distDir, 'index.html'), html);

  const jsSize = fs.statSync(path.join(distDir, 'radar.min.js')).size;
  const cssSize = fs.statSync(path.join(distDir, 'style.min.css')).size;
  console.log(`Built: radar.min.js (${(jsSize / 1024).toFixed(1)} KB) + style.min.css (${(cssSize / 1024).toFixed(1)} KB)`);
}

if (watch) {
  const ctx = esbuild.context({
    entryPoints: [path.join(__dirname, 'app.js')],
    bundle: true,
    minify: false,
    sourcemap: true,
    format: 'esm',
    outfile: path.join(distDir, 'radar.min.js'),
  });
  ctx.then(c => {
    c.watch();
    console.log('Watching for changes...');
  });
  build();
} else {
  build().catch(err => {
    console.error(err);
    process.exit(1);
  });
}
