// Unit tests for the web installer's front-end logic. Run:  node tests/test_ui_logic.js
// Loads lib/web/index.html's script in a sandbox with no DOM (boot() is skipped) and exercises pure functions.
const fs = require("fs"), vm = require("vm"), path = require("path"), assert = require("assert");
const html = fs.readFileSync(path.join(__dirname, "..", "lib", "web", "index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const ctx = vm.createContext({console});
vm.runInContext(script.replace("__TOKEN__", "t"), ctx);
const run = code => vm.runInContext(code, ctx);

let n = 0;
function test(name, fn) { try { fn(); n++; console.log("  ok  " + name); } catch (e) { console.log("FAIL  " + name + "\n      " + e.message); process.exitCode = 1; } }

const init = {hw: {cpu: {model: "Test CPU"}, ram_gb: 8, nic: {wired: true, speed_mbps: 1000}},
  drives: [{label: "Home folder", path: "/home/u", free_gb: 420, kind: "SSD"}, {label: "Data", path: "/mnt/data", free_gb: 1850, kind: "hard drive"}],
  home: "/home/u", saved: true, timezone: "Europe/Amsterdam"};

test("fresh state has visible secrets and one empty debrid row", () => {
  run("S = freshState(); S.init = " + JSON.stringify(init));
  assert.strictEqual(run("S.hideSecrets"), false);
  assert.strictEqual(run("S.debrid.length"), 1);
});

test("login validation", () => {
  run("S = freshState()");
  assert.strictEqual(run("validateStep(1).length"), 2);
  run("S.admin_user='bad name!'; S.admin_pass='short'");
  assert.strictEqual(run("validateStep(1).length"), 2);
  run("S.admin_user='alex'; S.admin_pass='Passw0rd!123'");
  assert.strictEqual(run("validateStep(1).length"), 0);
});

test("library validation needs a folder and a sane name", () => {
  run("S = freshState()");
  assert.ok(run("validateStep(2).length") >= 1);
  run("S.library.base='/home/u'");
  assert.strictEqual(run("validateStep(2).length"), 0);
  run("S.library.name='a/b'");
  assert.strictEqual(run("validateStep(2).length"), 1);
});

test("debrid needs at least one key; blank rows are ignored in the payload", () => {
  run("S = freshState()");
  assert.strictEqual(run("validateStep(3).length"), 1);
  run("S.debrid=[{provider:'alldebrid',api_key:' KEY1 '},{provider:'torbox',api_key:''},{provider:'realdebrid',api_key:'KEY2'}]");
  assert.strictEqual(run("validateStep(3).length"), 0);
  const p = run("buildPayload()");
  assert.deepStrictEqual(JSON.parse(JSON.stringify(p.debrid)), [{provider: "alldebrid", api_key: "KEY1"}, {provider: "realdebrid", api_key: "KEY2"}]);
});

test("optional sections only validate when switched on", () => {
  run("S = freshState()");
  for (const i of [4, 5, 6]) assert.strictEqual(run("validateStep(" + i + ").length"), 0);
  run("S.arabic.on=true"); assert.strictEqual(run("validateStep(5).length"), 1);
  run("S.arabic.username='u'; S.arabic.password='p'; S.arabic.tmdb='k'"); assert.strictEqual(run("validateStep(5).length"), 0);
  run("S.subs.opensubtitlescom.on=true"); assert.strictEqual(run("validateStep(6).length"), 1);
  run("S.subs.opensubtitlescom.username='me'; S.subs.opensubtitlescom.password='pw'"); assert.strictEqual(run("validateStep(6).length"), 0);
  run("S.arabtorrents.on=true"); assert.strictEqual(run("validateStep(4).length"), 1);
});

test("cache choice: size must fit the drive", () => {
  run("S = freshState(); S.cache={candidates:[{mount:'/',dir:'/home/u',free_gb:400,iops:9000,suggested_gb:200}],min_free_gb:40}; S.cache_choice=0; S.cache_size=200");
  assert.strictEqual(run("validateStep(7).length"), 0);
  run("S.cache_size=900"); assert.strictEqual(run("validateStep(7).length"), 1);
  run("S.cache_size=2"); assert.strictEqual(run("validateStep(7).length"), 1);
  run("S.cache_choice=-1"); assert.strictEqual(run("validateStep(7).length"), 0);
  const p = JSON.parse(JSON.stringify(run("S.cache_choice=0; S.cache_size=150; buildPayload()")));
  assert.deepStrictEqual(p.cache, {dir: "/home/u", size_gb: 150});
  assert.deepStrictEqual(JSON.parse(JSON.stringify(run("S.cache_choice=-1; buildPayload().cache"))), {});
});

test("applySaved restores a previous answers file", () => {
  run("S = freshState()");
  const saved = {admin_user: "alex", admin_pass: "Passw0rd!123", media_root: "/mnt/data/Media", debrid: [{provider: "torbox", api_key: "K"}],
    indexer_accounts: {arabp2p: {username: "ap", password: "pp"}, arabtorrents: {username: "at", password: "pw"}}, enable_arabarr: true, tmdb_api_key: "TM",
    subtitles: {subdl: {api_key: "SD"}}};
  assert.strictEqual(run("applySaved(" + JSON.stringify(saved) + ")"), true);
  assert.strictEqual(run("S.library.base"), "/mnt/data");
  assert.strictEqual(run("S.library.name"), "Media");
  assert.strictEqual(run("S.arabic.tmdb"), "TM");
  assert.strictEqual(run("S.subs.subdl.on"), true);
  assert.strictEqual(run("S.subs.subsource.on"), false);
  assert.strictEqual(run("applySaved({}, freshState())"), false);
});

test("every page renders without errors, in several states, with no 'undefined'/'NaN'", () => {
  const states = [
    "S = freshState(); S.init=" + JSON.stringify(init),
    "S = freshState(); S.init=" + JSON.stringify(init) + "; S.arabic.on=S.arabtorrents.on=S.arabicsource.on=true; for (const k in S.subs) S.subs[k].on=true; S.hideSecrets=true",
    "S = freshState(); S.init=" + JSON.stringify(init) + "; S.cacheLoading=true",
    "S = freshState(); S.init=" + JSON.stringify(init) + "; S.cache={candidates:[],min_free_gb:40}",
    "S = freshState(); S.init=" + JSON.stringify(init) + "; S.cache={candidates:[{mount:'/',dir:'/h',free_gb:400.4,iops:9000,suggested_gb:200}],min_free_gb:40}; S.cache_choice=0; S.cache_size=200; S.library.base='/home/u'; S.debrid=[{provider:'torbox',api_key:'K'},{provider:'alldebrid',api_key:'J'}]"
  ];
  for (const s of states) {
    run(s);
    for (let i = 0; i < 9; i++) {
      const out = run("PAGES[" + i + "]()");
      assert.strictEqual(typeof out, "string");
      assert.ok(out.length > 50, "page " + i + " too short");
      assert.ok(!/undefined|NaN/.test(out), "page " + i + " contains undefined/NaN");
    }
  }
});

test("user-typed text is escaped everywhere it is echoed (no HTML injection)", () => {
  run("S = freshState(); S.init=" + JSON.stringify(init));
  run("S.admin_user='<img src=x onerror=alert(1)>'; S.admin_pass='\"><script>x</script>'; S.library.base='/a\"b'; S.library.name='<b>'");
  for (const i of [1, 2, 8]) {
    const out = run("PAGES[" + i + "]()");
    assert.ok(!/<img src=x/.test(out) && !/<script>x/.test(out) && !/<b>[^<]*<\/b>'?$/.test("") , "page " + i + " leaked raw HTML");
    assert.ok(!out.includes('"><script>'), "page " + i + " leaked a script tag");
  }
  assert.strictEqual(run("esc('<&>\"\\'')"), "&lt;&amp;&gt;&quot;&#39;");
});

test("progress steps are derived from the log", () => {
  const lines = [{t: "head", text: "Checking this machine"}, {t: "ok", text: "Docker"}, {t: "head", text: "Starting containers"}, {t: "fail", text: "boom"}, {t: "head", text: "Never reached"}];
  const s = JSON.parse(JSON.stringify(run("stepsFromLog(" + JSON.stringify(lines) + ")")));
  assert.deepStrictEqual(s.map(x => x.st), ["ok", "fail", "run"]);
});

console.log("\n" + n + " front-end checks passed" + (process.exitCode ? " (WITH FAILURES)" : ""));
