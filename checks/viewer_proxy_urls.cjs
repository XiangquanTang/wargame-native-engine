/* Browser URL/bootstrap contract without external JS dependencies. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../src/wargame_native_engine/viewer/static');
const helper = fs.readFileSync(path.join(root, 'viewer_urls.js'), 'utf8');
for (const page of ['index.html', 'live_index.html', 'dual_control_index.html']) {
  const html = fs.readFileSync(path.join(root, page), 'utf8');
  assert(!html.includes('="/static/'));
  const bootstrap = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  for (const [pathname, expected] of [
    ['/', '/'], ['/red', '/'], ['/blue', '/'],
    ['/notebook/proxy/8003/red', '/notebook/proxy/8003/'],
    ['/notebook/proxy/8000/', '/notebook/proxy/8000/'],
    ['/notebook/proxy/8000', '/notebook/proxy/8000/'],
    ['/notebook/proxy/8000/index.html', '/notebook/proxy/8000/'],
  ]) {
    const location = new URL('https://example.test' + pathname + '?token=red%26secret#fragment');
    const document = {createElement: () => ({}), head: {appendChild(base) {document.baseURI = base.href;}}};
    const context = {URL, window: {location}, document};
    vm.createContext(context);
    vm.runInContext(bootstrap, context);
    vm.runInContext(helper, context);
    assert.equal(document.baseURI, 'https://example.test' + expected);
    assert.equal(context.ViewerUrls.resolve('/static/styles.css'), expected + 'static/styles.css');
    assert.equal(context.ViewerUrls.resolve('/api/status?token=red%26secret'), expected + 'api/status?token=red%26secret');
    for (const invalid of ['https://evil.test/api/x', '//evil.test/api/x', '/api/../../x', '/api/%2e%2e/x', '/api/%2f../x', '/api/a\\b', '/other']) {
      assert.throws(() => context.ViewerUrls.resolve(invalid), undefined, invalid);
    }
  }
}
console.log('Viewer mounts: root, seat, notebook prefix, trailing slash, token encoding and traversal checks passed');
