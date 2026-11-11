// Runs the REAL Apps Script files (Api.gs, Register.gs, Usage.gs from the package) in Node behind an HTTP server that behaves like a deployed web app:
// POST /exec?api=register|usage runs doPost and answers 302 to /echo/<id>; GET /echo/<id> returns the JSON (like script.googleusercontent.com).
// Sheets, cache, lock, properties and UrlFetchApp are in-memory mocks. GET /_dump shows the sheets (test helper).
// usage: node usage_server_sim.js <folder with the .gs files> <teamKey>      prints "PORT <n>" when ready
const fs = require('fs'), vm = require('vm'), http = require('http'), crypto = require('crypto');
const [, , gsDir, teamKey] = process.argv;

const sheets = {};
function mkSheet(name) {
  const rows = [];
  const sh = {
    name, rows,
    getLastRow: () => rows.length,
    appendRow: (r) => { rows.push(r.slice()); },
    setFrozenRows() {}, setTabColor() {}, setColumnWidths() {},
    getRange(r, c, nr, nc) {
      nr = nr || 1; nc = nc || 1;
      const rg = {
        getValues: () => { const o = []; for (let i = 0; i < nr; i++) { const row = []; for (let j = 0; j < nc; j++) { const v = (rows[r - 1 + i] || [])[c - 1 + j]; row.push(v === undefined ? '' : v); } o.push(row); } return o; },
        setValues: (vals) => { vals.forEach((vr, i) => vr.forEach((v, j) => { (rows[r - 1 + i] = rows[r - 1 + i] || [])[c - 1 + j] = v; })); return rg; },
        setValue: (v) => { (rows[r - 1] = rows[r - 1] || [])[c - 1] = v; return rg; },
        setFontWeight: () => rg, setBackground: () => rg, setFontColor: () => rg, setDataValidation: () => rg,
      };
      return rg;
    },
  };
  return sh;
}
const props = { USAGE_TEAM_KEY: teamKey, USAGE_ALLOWED_DOMAINS: 'tigeranalytics.com' };
const cacheStore = {};
const ctx = {
  console,
  SpreadsheetApp: { getActiveSpreadsheet: () => ({ getSheetByName: (n) => sheets[n] || null, insertSheet: (n) => (sheets[n] = mkSheet(n)) }),
    newDataValidation: () => ({ requireValueInList() { return this; }, build() { return {}; } }) },
  PropertiesService: { getScriptProperties: () => ({ getProperty: (k) => props[k] || null, setProperty: (k, v) => { props[k] = v; } }) },
  CacheService: { getScriptCache: () => ({ get: (k) => cacheStore[k] || null, put: (k, v) => { cacheStore[k] = v; } }) },
  LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock() {} }) },
  Utilities: {
    getUuid: () => crypto.randomUUID(),
    DigestAlgorithm: { SHA_256: 'sha256' }, Charset: { UTF_8: 'utf8' },
    computeDigest: (a, s) => Array.from(crypto.createHash('sha256').update(s, 'utf8').digest()).map((b) => (b > 127 ? b - 256 : b)),
  },
  UrlFetchApp: { fetch: (url) => {
    const name = decodeURIComponent(url.split('/').pop());
    return { getResponseCode: () => (name === 'nosuchuser' ? 404 : name === 'ratelimited' ? 403 : 200) };
  } },
  ContentService: { MimeType: { JSON: 'json' }, createTextOutput: (t) => ({ t, setMimeType() { return this; } }) },
  ScriptApp: { getService: () => ({ getUrl: () => '' }) },
};
vm.createContext(ctx);
['Api.gs', 'Register.gs', 'Usage.gs'].forEach((f) => vm.runInContext(fs.readFileSync(gsDir + '/' + f, 'utf8'), ctx, { filename: f }));

const echo = {};
http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  if (req.method === 'POST' && url.pathname === '/exec') {
    const parameter = Object.fromEntries(url.searchParams);
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      const out = ctx.doPost({ parameter, postData: { contents: body, type: req.headers['content-type'] } });
      const id = crypto.randomBytes(6).toString('hex');
      echo[id] = out.t;
      res.writeHead(302, { Location: '/echo/' + id }); res.end();
    });
  } else if (req.method === 'GET' && url.pathname.startsWith('/echo/')) {
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(echo[url.pathname.slice(6)] || '{}');
  } else if (req.method === 'GET' && url.pathname === '/exec') {
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(ctx.apiPing_().t);
  } else if (url.pathname === '/_clearhash') {   // what an admin does by hand in the sheet
    const u = (sheets['Claude_Users'].rows.find((r) => r[0] === url.searchParams.get('u')) || []); u[4] = '';
    res.writeHead(200); res.end('ok');
  } else if (url.pathname === '/_dump') {
    const d = {}; Object.keys(sheets).forEach((k) => (d[k] = sheets[k].rows));
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(d));
  } else { res.writeHead(404); res.end(); }
}).listen(0, '127.0.0.1', function () { console.log('PORT ' + this.address().port); });
