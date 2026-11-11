// Runs the REAL Pull.gs (Apps Script) in Node against a real `cckit server` over HTTP (curl stands in for UrlFetchApp).
// Sheets and Script Properties are in-memory mocks that persist in a JSON state file between invocations, like a spreadsheet would.
// usage: node puller_sim.js <folder with Pull.gs> <state.json> <serverUrl> <adminToken> <action> [json args]
//   action: pull | scratch | setup (args: ["url answer","token answer"])      prints one JSON line {result|error, sheets, props, triggers, alerts}
const fs = require('fs'), vm = require('vm'), cp = require('child_process');
const [, , dir, statePath, serverUrl, adminToken, action, argJson] = process.argv;
const st = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, 'utf8')) : { sheets: {}, props: serverUrl ? { USAGE_SERVER_URL: serverUrl, USAGE_ADMIN_TOKEN: adminToken } : {}, triggers: [] };
const alerts = [];

function mkSheet(name) {
  const rows = st.sheets[name] = st.sheets[name] || [];
  const sh = {
    getLastRow: () => rows.length, getLastColumn: () => Math.max(0, ...rows.map((r) => r.length)),
    setFrozenRows() {}, setTabColor() {},
    getRange(r, c, nr, nc) {
      nr = nr || 1; nc = nc || 1;
      const rg = {
        setValues: (vals) => { vals.forEach((vr, i) => vr.forEach((v, j) => { (rows[r - 1 + i] = rows[r - 1 + i] || [])[c - 1 + j] = v; })); return rg; },
        clearContent: () => { for (let i = 0; i < nr; i++) rows[r - 1 + i] = []; for (let i = rows.length; i > 0 && !(rows[i - 1] || []).length; i--) rows.pop(); return rg; },
        setFontWeight: () => rg, setBackground: () => rg, setFontColor: () => rg,
      };
      return rg;
    },
  };
  return sh;
}
const ss = { getSheetByName: (n) => (st.sheets[n] ? mkSheet(n) : null), insertSheet: (n) => mkSheet(n) };
const answers = argJson ? JSON.parse(argJson) : [];
const ui = {
  ButtonSet: { OK_CANCEL: 'oc' }, Button: { OK: 'OK' },
  prompt: () => { const a = answers.shift(); return { getSelectedButton: () => (a === null || a === undefined ? 'CANCEL' : 'OK'), getResponseText: () => a || '' }; },
  alert: (m) => alerts.push(m),
};
const ctx = {
  console, Date, Number, JSON, Error, Math, String, Object,
  SpreadsheetApp: { getActiveSpreadsheet: () => ss, getUi: () => ui },
  PropertiesService: { getScriptProperties: () => ({ getProperty: (k) => (k in st.props ? st.props[k] : null), setProperty: (k, v) => { st.props[k] = v; }, deleteProperty: (k) => { delete st.props[k]; } }) },
  LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock() {} }) },
  ScriptApp: {
    getProjectTriggers: () => st.triggers.map((t) => ({ getHandlerFunction: () => t.fn, _t: t })),
    deleteTrigger: (t) => { st.triggers = st.triggers.filter((x) => x !== t._t); },
    newTrigger: (fn) => { const t = { fn }; return { timeBased() { return this; }, everyMinutes(n) { t.every = n; return this; }, create() { st.triggers.push(t); } }; },
  },
  UrlFetchApp: { fetch: (url, opt) => {
    // setup only accepts https, but the test server is plain http on localhost: PULL_SIM_REWRITE="https://usage.test|http://127.0.0.1:PORT"
    if (process.env.PULL_SIM_REWRITE) { const [from, to] = process.env.PULL_SIM_REWRITE.split('|'); url = url.replace(from, to); }
    const out = cp.execFileSync('curl', ['-s', '-m', '15', '-w', '\n%{http_code}', '-H', 'Authorization: ' + ((opt.headers || {}).Authorization || ''), url], { encoding: 'utf8' });
    const i = out.lastIndexOf('\n');
    return { getResponseCode: () => Number(out.slice(i + 1)), getContentText: () => out.slice(0, i) };
  } },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(dir + '/Pull.gs', 'utf8'), ctx, { filename: 'Pull.gs' });
let result, error;
try {
  result = action === 'setup' ? ctx.setupUsagePull() : action === 'scratch' ? ctx.pullClaudeUsageFromScratch() : ctx.pullClaudeUsage();
} catch (e) { error = e.message; }
fs.writeFileSync(statePath, JSON.stringify(st));
console.log(JSON.stringify({ result, error, sheets: st.sheets, props: st.props, triggers: st.triggers, alerts }));
